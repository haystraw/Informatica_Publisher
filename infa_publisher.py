import os
import csv
import json
import requests
import getpass
import sys
import re
import configparser
import argparse
import ast
import time
from datetime import datetime, timedelta
from requests_toolbelt.multipart.encoder import MultipartEncoder

version = 20250130
print(f"INFO: Informatica CDGC Publisher {version}")
help_message = '''
Optionally, you can set parameters:

   --help
        Displays all of the command line arguments.

   --default_user
        You can specify the username to use. if you do now specify one, it will look in the
        ~/.informatica_cdgc/credentials file (as shown below)
        Example:
            --default_user=shayes_compass

   --default_pwd
        You can specify the password to use. if you do now specify one, it will look in the
        ~/.informatica_cdgc/credentials file (as shown below)
        Example:
            --default_pwd=12345

   --default_pod
        You can specify the pod to use. if you do now specify one, it will look in the
        ~/.informatica_cdgc/credentials file (as shown below)
        Typically this "pod" can be shown in the url: for example: "dm-us"
        Example:
            --default_pwd=dm-us  

   --csv_file
        You can specify the config csv file to use directly, by setting it here. 
        It will default to the directory where the script/exe file resides.
        Example:
            --csv_file=my_classifications.csv

   --csv_file_path
        You can specify the config csv file to use directly, by setting it here. 
        Setting it with this option, will allow you to specify the full path (use linux forward slashes)
        Example:
            --csv_file_path=c:/junk/my_classifications.csv 

    --extracts_folder
        You can specify a different location to write the temporary files that the api downloads.
        These files are raw collection of resources, and assets (in the case of using the api)
        It will default to <script_location>/extracts 
        Example:
            --extracts_folder=/my_files/data

    --templates_folder
        You can specify a different location where the template files exist
        It will default to <script_location>/templates 
        Example:
            --templates_folder=/my_files/templates
   
'''


default_pod="dmp-us"        
default_user=""
default_pwd=""


prompt_for_login_info = True
pause_before_loading = True
create_payloads_only = False
when_extracting_fetch_details = True
show_raw_errors = False
pause_at_end = True
skip_existing_lookup_tables = False


# Paths
script_location = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
csv_file_path = script_location+'/xxxxxxxxxxxxxxxxxxxxx.csv'
csv_file = ''
templates_folder = script_location+'/templates'
extracts_folder = script_location+'/extracts'

# Get the current timestamp for folder creation
timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
payloads_folder = f'{script_location}/payloads/payloads_{timestamp}'

# Ensure the payloads directory exists
os.makedirs(payloads_folder, exist_ok=True)

total_payloads_to_load = []

created_files = set()

def parse_parameters():
    # Check for --help first
    if '--help' in sys.argv:
        print(help_message)
        programPause = input("Press the <ENTER> key to exit...")
        sys.exit(0)

    parser = argparse.ArgumentParser(description="Dynamically set variables from command-line arguments.")
    args, unknown_args = parser.parse_known_args()

    for arg in unknown_args:
        if arg.startswith("--") and "=" in arg:
            key, value = arg[2:].split("=", 1)  # Remove "--" and split into key and value
            try:
                # Safely parse value as Python object (list, dict, etc.)
                value = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                pass  # Leave value as-is if parsing fails

            # Handle appending to arrays or updating dictionaries
            if key in globals():
                existing_value = globals()[key]
                if isinstance(existing_value, list) and isinstance(value, list):
                    ## If what was passed is an array, we'll append to the array
                    existing_value.extend(value)  # Append to the existing array
                elif isinstance(existing_value, dict) and isinstance(value, dict):
                    ## If what was passed is a dict, we'll add to the dict
                    existing_value.update(value)  # Add or update keys in the dictionary
                else:
                    ## Otherwise, it's an ordinary variable. replace it
                    globals()[key] = value  # Replace for other types

            else:
                ## It's a new variable. Create an ordinary variable.
                globals()[key] = value  # Set as a new variable


def select_recent_csv(directory):
    """
    Lists CSV files in a given directory, sorted by most recent modification time,
    prompts the user to select one, and returns the path for the selected file.

    Args:
        directory (str): The directory to search for CSV files.

    Returns:
        str: The full path of the selected CSV file, or None if no valid file is selected.
    """
    # Expand user directory if ~ is used
    directory = os.path.expanduser(directory)

    # Check if the directory exists
    if not os.path.isdir(directory):
        print(f"Directory not found: {directory}")
        return None

    # List all CSV files in the directory
    csv_files = [
        os.path.join(directory, file)
        for file in os.listdir(directory)
        if file.endswith('.csv')
    ]

    # Check if any CSV files were found
    if not csv_files:
        print(f"No CSV files found in the directory: {directory}")
        return None

    # Sort the files by modification time (most recent first)
    csv_files.sort(key=os.path.getmtime, reverse=True)

    # Display the files to the user with their modification times
    print("Select a CSV file:")
    for i, file in enumerate(csv_files, start=1):
        ## mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(file))
        print(f"    {i}. {os.path.basename(file)}")

    # Prompt the user to select a file
    while True:
        try:
            choice = int(input(f"Enter the number of the file to select (1-{len(csv_files)}): "))
            if 1 <= choice <= len(csv_files):
                selected_file = csv_files[choice - 1]
                return selected_file
            else:
                print(f"Invalid choice. Please select a number between 1 and {len(csv_files)}.")
        except ValueError:
            print("Invalid input. Please enter a number.")


def process_json_error(text):
    result_text = text
    if not show_raw_errors:
        try:
            resultJson = json.loads(text)
            result_text = resultJson['message']
        except Exception as e:
            pass
    return result_text


def load_credentials_from_home():
    global default_user, default_pwd, default_pod

    def get_informatica_credentials():
        credentials_path = os.path.join(os.path.expanduser("~"), ".informatica_cdgc", "credentials")
        if not os.path.exists(credentials_path):
            print(f"Credentials file not found: {credentials_path}")
            return None

        config = configparser.ConfigParser()
        config.read(credentials_path)

        if "default" in config:
            return dict(config["default"])

        # If no default section, list available profiles and prompt user to select one
        profiles = config.sections()
        if not profiles:
            return None

        print("INFO: No 'default' profile found. Please select a profile:")
        for i, profile in enumerate(profiles, start=1):
            print(f"    {i}. {profile}")

        # Prompt user for selection
        while True:
            try:
                choice = int(input("Enter the number of the profile to use: "))
                if 1 <= choice <= len(profiles):
                    selected_profile = profiles[choice - 1]
                    print(f"Using credentials from the '{selected_profile}' profile.")
                    return dict(config[selected_profile])
                else:
                    print(f"INFO: Invalid choice. Please select a number between 1 and {len(profiles)}.")
            except ValueError:
                print("INFO: Invalid input. Please enter a valid number.")

    if len(default_user) < 1 or len(default_pwd) < 1 or len(default_pod) < 1:
        credentials_dict = get_informatica_credentials()
        if credentials_dict:
            default_user = credentials_dict.get('user')
            default_pwd = credentials_dict.get('pwd')
            default_pod = credentials_dict.get('pod')
        else:
            # Define the path to the credentials file in the user's home directory
            credentials_path = os.path.join(os.path.expanduser("~"), ".informatica_cdgc", "credentials.json")
            
            # Check if the file exists
            if os.path.exists(credentials_path):
                with open(credentials_path, 'r') as file:
                    try:
                        # Load the JSON data
                        credentials = json.load(file)
                        
                        # Set each credential individually if it exists in the file
                        if 'default_user' in credentials:
                            default_user = credentials['default_user']
                        if 'default_pwd' in credentials:
                            default_pwd = credentials['default_pwd']
                        if 'default_pod' in credentials:
                            default_pod = credentials['default_pod']
                        
                    except json.JSONDecodeError:
                        pass

def getCredentials():
    global pod
    global iics_user
    global iics_pwd
    global iics_url
    global cdgc_url
    
    load_credentials_from_home()

    if any(var not in globals() for var in ['pod', 'iics_user', 'iics_pwd', 'iics_url', 'cdgc_url']):
        if prompt_for_login_info == True:
            pod = input(f"Enter pod (default: {default_pod}): ") or default_pod
            iics_user = input(f"Enter username (default : {default_user}): ") or default_user
            iics_pwd=getpass.getpass("Enter password: ") or default_pwd   
        else:
            if len(default_pod) > 1:
                pod = default_pod
            else:
                pod = input(f"Enter pod (default: {default_pod}): ") or default_pod
            if len(default_user) > 1:
                iics_user = default_user
            else:
                iics_user = input(f"Enter username (default : {default_user}): ") or default_user
            if len(default_pwd) > 1:
                iics_pwd = default_pwd
            else:
                iics_pwd=getpass.getpass("Enter password: ") or default_pwd   
        iics_url = "https://"+pod+".informaticacloud.com"
        cdgc_url = "https://cdgc-api."+pod+".informaticacloud.com"

def login():
    global sessionID
    global orgID
    global headers
    global headers_bearer
    global jwt_token
    global api_url   
    # retrieve the sessionID & orgID & headers
    ## Test to see if I'm already logged in
    if 'jwt_token' not in globals() or len(headers_bearer) < 2:
        loginURL = iics_url+"/saas/public/core/v3/login"
        loginData = {'username': iics_user, 'password': iics_pwd}
        response = requests.post(loginURL, headers={'content-type':'application/json'}, data=json.dumps(loginData))
        try:        
            data = json.loads(response.text)   
            sessionID = data['userInfo']['sessionId']
            orgID = data['userInfo']['orgId']
            api_url = data['products'][0]['baseApiUrl']
            headers = {'Accept':'application/json', 'INFA-SESSION-ID':sessionID,'IDS-SESSION-ID':sessionID, 'icSessionId':sessionID}
        except:
            print("ERROR: logging in: ",loginURL," : ",response.text)
            quit()

        # retrieve the Bearer token
        URL = iics_url+"/identity-service/api/v1/jwt/Token?client_id=cdlg_app&nonce=g3t69BWB49BHHNn&access_code="  
        response = requests.post(URL, headers=headers, data=json.dumps(loginData))
        try:        
            data = json.loads(response.text)
            jwt_token = data['jwt_token']
            headers_bearer = {'content-type':'application/json', 'Accept':'application/json', 'INFA-SESSION-ID':sessionID,'IDS-SESSION-ID':sessionID, 'icSessionId':sessionID, 'Authorization':'Bearer '+jwt_token}        
        except:
            print("ERROR: Getting Token in: ",URL," : ",response.text)
            quit()

def run_elastic_search_from_template(input_file="", export_file=f"{extracts_folder}/assets.json"):
    try:
        with open(input_file, 'r', encoding='utf-8') as file:
            file_contents = file.read()

        result_json = run_elastic_search(file_contents)

        os.makedirs(extracts_folder, exist_ok=True)
        '''
        Commenting out. We'll overwrite the file if it's there.
        if os.path.exists(export_file) and os.path.getsize(export_file) > 0:
            # Read existing data
            with open(export_file, 'r') as file:
                try:
                    data = json.load(file)  # Load existing JSON array
                except json.JSONDecodeError:
                    data = []  # Start a new array if the file is not valid JSON
        else:
        '''
        data = []  # Start a new array if the file doesn't exist or is empty

        # Append new data to the list
        data.append(result_json)

        # Write the updated array back to the file
        with open(export_file, 'w') as file:
            json.dump(data, file, indent=4)
        print(f"INFO: Wrote information to {export_file}")
    except Exception as e:
        print(f"ERROR: Error reading {input_file}: {e}")


def run_elastic_search(payload):
    getCredentials()
    login()
    this_header = headers_bearer.copy()
    this_header['X-INFA-SEARCH-LANGUAGE'] = 'elasticsearch'
    
    Result = requests.post(cdgc_url+"/ccgf-searchv2/api/v1/search", headers=this_header, data=payload)
    detailResultJson = json.loads(Result.text)
    return detailResultJson

def fetch_id(name="", header="", assets_file=f"{extracts_folder}/assets.json"):
    type_name = "ANY"
    if ":" in header:
        type_name = header.split(":")[1]

    # Load the JSON file
    with open(assets_file, 'r', encoding='utf-8') as file:
        data = json.load(file)

    # Traverse the JSON structure
    for hit in data[0]["hits"]["hits"]:  # Adjust this indexing if needed
        source = hit.get("sourceAsMap", {})

        core_name = source.get("core.name", "")
        core_class_type = source.get("core.classType", "")
        core_identity = source.get("core.identity", "")

        # Check if conditions are met
        if core_name == name and ( type_name in core_class_type or type_name == "ANY" ):
            return core_identity

    return "NO_ID_FOUND"

def publish_from_template(template_file="", token_replacements={}, publish_info=""):
    try:
        with open(template_file, 'r', encoding='utf-8') as file:
            file_contents = file.read()

        # Perform replacements using dictionary keys
        for key, value in token_replacements.items():
            file_contents = file_contents.replace(f"{{{key}}}", value)

        # Call publish function with modified contents
        result_json = publish(file_contents, publish_info=publish_info)

    except Exception as e:
        print(f"ERROR: Error reading {template_file}: {e}")
   

def publish(payload, publish_info=""):

    getCredentials()
    login()
    this_header = headers_bearer
    this_header['X-INFA-SEARCH-LANGUAGE'] = 'elasticsearch'
    this_header['X-INFA-PRODUCT-ID'] = 'CDGC'
    try:
        Result = requests.post(cdgc_url+"/ccgf-contentv2/api/v1/publish", headers=this_header, data=payload) 
        detailResultJson = json.loads(Result.text)
        try:
            message_code =  detailResultJson['items'][0]['messageCode']
            print(f"INFO: Publish ({publish_info}) result: {message_code}")
        except:
            print(f"ERROR: Error publishing ({publish_info}) {Result.text}")
    except Exception as e:
        print(f"ERROR: Error publishing ({publish_info}): {e}")
        return Result.text


def main():
    global csv_file_path, total_payloads_to_load, default_delete_csv_file

    parse_parameters()
    if len(csv_file) > 2:
        csv_file_path = script_location+'/'+csv_file

    if len(sys.argv) > 1:
        if os.path.exists(sys.argv[1]):
            csv_file_path = sys.argv[1]

    if not os.path.exists(csv_file_path):
        csv_file_path = select_recent_csv(script_location)


    with open(csv_file_path, mode='r') as csvfile:
        reader = csv.DictReader(csvfile)
        headers = reader.fieldnames

        searches_already_run = []
        # Process each row in the CSV
        for row in reader:
            search_template_name = row.get('Asset Search Template', '')+".json"
            publish_template_name = row.get('Publish Template')+".json"

            if search_template_name not in searches_already_run and len(search_template_name) > 6:
                run_elastic_search_from_template(input_file=f"{templates_folder}/{search_template_name}")
                searches_already_run.append(search_template_name)

            token_replacements = {}
            publish_info_array = []
            for header_name in headers:
                if header_name not in ('Asset Search Template', 'Publish Template'):
                    token_replacements[header_name] = row.get(header_name)
                    publish_info_array.append(f"{header_name}={row.get(header_name)}")
                    if header_name.startswith("id:"):
                        new_value = fetch_id(name=row.get(header_name), header=header_name)
                        token_replacements[header_name] = new_value
            publish_info_string = " | ".join(publish_info_array)
            publish_from_template(template_file=f"{templates_folder}/{publish_template_name}", token_replacements=token_replacements, publish_info=publish_info_string)


    if pause_at_end:
        input(f"Press any Key to exit...")
def print_message_loop(message, state=None, is_first_message=False, is_final_message=False):
    """
    Prints messages, appending dots for repeated messages, or resets for new messages.
    """
    if state is None:
        state = {"last_message": None, "repeat_counter": 0}

    if message == state["last_message"]:
        if state["repeat_counter"] < 30:
            state["repeat_counter"] += 1
            print('.', end='', flush=True)
        else:
            state["repeat_counter"] = 1
            print(f"\n{message}", end='', flush=True)
    else:
        if not is_first_message:
            print()  # Print a new line for a new message
        state["repeat_counter"] = 0
        print(message, end='', flush=True)

    state["last_message"] = message

    if is_final_message:
        print()  # Print a final newline

def monitor_job(jobId):
    """
    Monitors a job by polling its status every 10 seconds until completion or failure.
    """
    try:
        tracking_url = f"{cdgc_url}/ccgf-orchestration-management-api-server/api/v1/jobs/{jobId}?aggregateResourceUsage=false&expandChildren="

        state = {"last_message": None, "repeat_counter": 0}
        job_loop = True

        while job_loop:
            time.sleep(10)
            get_url = tracking_url
            this_header = headers_bearer.copy()
            response = requests.get(get_url, headers=this_header)

            if response.status_code in [200, 201, 202, 204]:
                result_json = response.json()
                job_status = result_json.get('status')

                # Check terminal states
                if any(term in job_status for term in ["COMPLETED", "ERROR", "CANCELLED", "FAILED"]):
                    print_message_loop(f"    {job_status}", state=state, is_final_message=True)
                    job_loop = False
                else:
                    print_message_loop(f"    {job_status}", state=state)
            else:
                print(f"\nERROR: with tracking the scan: {response.text}")
                job_loop = False  # Exit the loop on failure
    except Exception as e:
        print(f"\nERROR: with tracking the scan: {e}")


if __name__ == "__main__":
    main()                
