# run.py
#
# Runs the AnnTools pipeline
#
# NOTE: This file lives on the AnnTools instance and
# replaces the default AnnTools run.py
##


import sys
import time
import driver
import os
import json

import boto3
from botocore.exceptions import ClientError

# Get configuration
from configparser import ConfigParser, ExtendedInterpolation

config = ConfigParser(os.environ, interpolation = ExtendedInterpolation())
config_path = os.path.join(os.path.dirname(__file__), "annotator_config.ini")
config.read(config_path)
COMPLETE_TIME = config["gas"]["COMPLETE_TIME"]
REGION = config["aws"]["AwsRegionName"]
# print(config.sections())

"""A rudimentary timer for coarse-grained profiling
"""


class Timer(object):
    def __init__(self, verbose = True):
        self.verbose = verbose

    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, *args):
        self.end = time.time()
        self.secs = self.end - self.start
        if self.verbose:
            print(f"Approximate runtime: {self.secs:.2f} seconds")




# Upload the output files of a given annotation job
# Delete job related files on the Anntools instance after completion of uploads
# Update job status into "COMPLETED" only if the previous status is "RUNNING"
def S3Upload_LocalDelete_DynamoUpdate(target_file_path):

    s3 = boto3.client("s3", region_name = REGION)
    dynamo = boto3.client("dynamodb", region_name = REGION)
    sns = boto3.client("sns", region_name = REGION)
    sfn  = boto3.client("stepfunctions", region_name = REGION)

    results_bucket_name = config["s3"]["ResultsBucketName"]
    dy_table_name = config["dynamodb"]["AnnotationsTable"]
    prefix = config["DEFAULT"]["CnetId"]

    # target_file_path: ~/gas/ann/jobs/<user_id>/uuid~filename
    user_dir = os.path.dirname(target_file_path) # ~/gas/ann/jobs/<user_id>
    user_id = os.path.basename(user_dir) # user id
    file_name = os.path.basename(target_file_path) # uuid~filename
    job_id, input_filename = file_name.split('~', 1) # job_id = uuid, input_filename = filename

    # os.path.splitext(): https://docs.python.org/3/library/os.path.html#os.path.split
    rootname = os.path.splitext(input_filename)[0] # e.g., "test" for test.vcf

    # Define local paths for annotated and log files
    annotated_file_path = os.path.join(user_dir, job_id + "~" + rootname + config["ann"]["ANNOTFILE_SUFFIX"])
    annotated_file_name = os.path.basename(annotated_file_path)

    log_file_path = os.path.join(user_dir, job_id + "~" + rootname + config["ann"]["LOGFILE_SUFFIX"])
    log_file_name = os.path.basename(log_file_path)

    # Define the S3 key prefix
    annotated_key = f"{prefix}/{user_id}/{annotated_file_name}"
    log_key = f"{prefix}/{user_id}/{log_file_name}"

    # Check if the upload file can be found on the instance
    if not os.path.exists(annotated_file_path) or not os.path.exists(log_file_path):
        print(" Upload files not found on the server")
        sys.exit(1)


    # Catch failure to upload results/log files to S3
    try:
        # Upload the annotated file to S3 results bucket
        # Upload the log file to S3 results bucket
        # S3.Client.upload_file: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/upload_file.html
        s3.upload_file(annotated_file_path, results_bucket_name, annotated_key)
        s3.upload_file(log_file_path, results_bucket_name, log_key)

        # Delete local job files on the AnnTools instance
        os.remove(target_file_path)
        os.remove(annotated_file_path)
        os.remove(log_file_path)
# os.rmdir(user_dir)

        # Record the completion time right after result files are uploaded to S3 and local files are removed
        COMPLETE_TIME = int(time.time())  # Current time in epoch format

    # Catch the failure to upload output files to S3
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]
        print(f"{err_code}: Failed to upload S3 file object - {msg}")
        sys.exit(1)
        return

    # Catch the failure to delete local files on the AnnTools instance
    except OSError as e:
        print(f"OSError: Failed to delete local files or user directory - {str(e)}")
        sys.exit(1)
        return

    except FileNotFoundError as e:
        print(f"FileNotFoundError: Local files or user directory does not exist - {str(e)}")
        sys.exit(1)
        return

    # Catch the unexpected errors when removing files
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        sys.exit(1)
        return

    # Add attributes for the job item
    updateExpression = """SET s3_results_bucket = :rb,
                            s3_key_result_file = :ak, 
                            s3_key_log_file = :lk, 
                            complete_time = :ct, 
                            job_status = :js"""

    # Update the job item in DynamoDB table
    try:
        # DynamoDB.Client.update_item(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb/client/update_item.html
        # https://hands-on.cloud/boto3/dynamodb/update-item/#example-updating-a-single-item-using-boto-3-client
        dynamo.update_item(

                TableName = dy_table_name,

                # The primary key of the item to be updated
                Key = {"job_id": {"S": job_id}},

                # An expression that defines one or more attributes to be updated, the action to be performed on them, and new values for them
                UpdateExpression = updateExpression,

                # A condition that must be satisfied in order for a conditional update to succeed
                ConditionExpression = "job_status = :cond",

                # One or more values that can be substituted in an expression
                ExpressionAttributeValues = {
                    ":rb": {"S": results_bucket_name},
                    ":ak": {"S": annotated_key},
                    ":lk": {"S": log_key},
                    ":ct": {"N": str(COMPLETE_TIME)}, # DynamoDB stores epoch time as Number
                    ":js": {"S": "COMPLETED"},
                    ":cond": {"S": "RUNNING"}
                }
        )


    # Error handling with DynamoDB: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Programming.Errors.html
    # DynamoDB Developer Guide: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Programming.Errors.html
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code == "ConditionalCheckFailedException":
            print(f"{err_code}: Conditional check failed for job - {msg}")
            sys.exit(1)
            return

        elif err_code in ("ProvisionedThroughputExceededException", "ThrottlingException"):
            print(f"{err_code}: Rate of requests exceeds the allowed throughput - {msg}")
            sys.exit(1)
            return

        elif err_code == "ResourceNotFoundException":
            print(f"{err_code}: DynamoDB Table or item with job_id not found - {msg}")
            sys.exit(1)
            return

        elif err_code == "InternalServerError":
            print(f"{err_code}: Failed to update item in DynamoDB Table - {msg}")
            sys.exit(1)
            return

        else:
            print(f"Unexpected error: {err_code} - {msg}")
            sys.exit(1)


    # Prepare result message data
    email_data = {
        "job_id": job_id, # for displaying annotation job
        "user_id": user_id, # for calling helpers.py's get_user_profile() in notify.py
        "complete_time": COMPLETE_TIME # for displaying annotation job complete time im the email notification
    }


    try:
        # SNS.Client.publish(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sns/client/publish.html
        sns_response = sns.publish(

            # Specify the Amazon Resource Name (ARN) of the SNS topic to which our message is being published
            TopicArn = config["sns"]["TopicArn_Results"],

            # The actual content to be send
            # Convert the Message to a string, this is required!
            # If MessageStructure = "json", the value of the Message parameter must:
                # contain at least a top-level JSON key of “default” with a value that is a string
            # Non-string values within the JSON message will cause the key to be ignored.
            # json.dumps(): https://docs.python.org/3/library/json.html#json.dumps
            # NOTE: json.loads(json.dumps(x)) != x if x has non-string keys
            Message = json.dumps({"default": json.dumps(email_data)}),

            # More flexible
            MessageStructure = "json"

        )


        # When a messageId is returned, the message is saved
        # And Amazon SNS immediately delivers it to subscribers
        # See https://docs.aws.amazon.com/sns/latest/api/API_Publish.html
        if not sns_response.get("MessageId"):
            print(f"Failed to publish email message to SNS from run.py - No MessageId returned")
            sys.exit(1)


    # Catch exceptions ocurring while sending a notification message to the topic "jycchien_job_requests"
    # Botocore SNS Client Exceptions: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/services/sns.html#client-exceptions
    # SNS.Client.publish(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sns/client/publish.html
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code in ("InvalidParameter", "InvalidParameterValue"):
            print(f"{err_code}: Invalid parameter for the email publish to SNS - {msg}")
            sys.exit(1)

        elif err_code == "InternalError":
            print(f"{err_code}: Internal SNS error - {msg}")
            sys.exit(1)

        elif err_code == "NotFound":
            print(f"{err_code}: SNS topic not found - {msg}")
            sys.exit(1)

        else:
            print(f"Unexpected error when publish email message: {err_code} - {msg}")
            sys.exit(1)

    # Prepare archive message data
    archive_data = {
        "job_id": job_id, # for displaying annotation job
        "user_id": user_id, # for calling helpers.py's get_user_profile() in notify.py
        "complete_time": COMPLETE_TIME, # for displaying annotation job complete time im the email notification
        "s3_key_result_file": annotated_key,
        "s3_key_log_file": log_key
    }

    try:
        # Execute the State Machine for waiting 180 sec
        # What is Step Functions: https://docs.aws.amazon.com/step-functions/latest/dg/welcome.html
        # SFN.Client.start_execution(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/stepfunctions/client/start_execution.html
        response = sfn.start_execution(
            stateMachineArn = config["sfn"]["StateMachineArn"], # created through AWS Step Functions console
            input = json.dumps(archive_data)
        )

    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code in ("StateMachineDoesNotExist", "InvalidArn"):
            print(f"{err_code}: Specified State Machine Does Not Exist - {msg}")
            sys.exit(1)

        else:
            print(f"Unexpected error: {err_code} when executing state machine for waiting task - {msg}")
            sys.exit(1)



def main():

    if len(sys.argv) > 1:
        # Run the AnnTools pipeline
        with Timer():
            driver.run(sys.argv[1], "vcf")

        target_file_path = sys.argv[1]
        S3Upload_LocalDelete_DynamoUpdate(target_file_path)

    else:
        print("A valid .vcf file must be provided as input to this program.")




if __name__ == "__main__":
    main()

### EOF