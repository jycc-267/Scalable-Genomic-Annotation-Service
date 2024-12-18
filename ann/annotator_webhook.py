# annotator_webhook.py
#
# Modified to run as a web server that can be called by SNS to process jobs
# Run using: python annotator_webhook.py
#
# NOTE: This file lives on the AnnTools instance
##

import requests
from flask import Flask, jsonify, request

import boto3
import json
import os
import sys
import time
import subprocess
from botocore.exceptions import ClientError


#################### 
# Global Variables #
####################

app = Flask(__name__)
app.url_map.strict_slashes = False

# Get configuration and add to Flask app object
environment = "annotator_webhook_config.Config"
app.config.from_object(environment)


REGION = app.config["AWS_REGION_NAME"]

# Store static path variables
# os.path.expanduser(): https://docs.python.org/3/library/os.path.html#os.path.expanduser
ANN_DIR = os.path.expanduser(app.config["ANN_DIR"])
ANN_JOBS_DIR = os.path.expanduser(app.config["ANN_JOBS_DIR"])

SQS_QUEUE_URL_REQUESTS = app.config["SQS_QUEUE_URL_REQUESTS"]
MAX_MESSAGES = app.config["AWS_SQS_MAX_MESSAGES"]
WAIT_TIME = app.config["AWS_SQS_WAIT_TIME"]

ANNOTATIONS_TABLE = app.config["AWS_DYNAMODB_ANNOTATIONS_TABLE"]

# Create AWS clients
sqs = boto3.client("sqs", region_name = REGION)
s3 = boto3.client("s3", region_name = REGION)
dynamo = boto3.client("dynamodb", region_name = REGION)

# Create a dierectory for storing each input file download
# Directory already exists is acceptable
# os.mkdirs(): https://docs.python.org/3/library/os.html#os.mkdirs
os.makedirs(ANN_JOBS_DIR, exist_ok = True)


###################
# Helper Function #
###################


# Extract job parameters from a message body
# Download the input file from S3 object into a local file on EC2 instance
# Spwan multiple annotation jobs as background processes concurrently
# Delete the message from the queue, if job was successfully submitted
def process_message(message):

    messageID = message.get("MessageId")
    print(f"Start processing message: {messageID}")

    try:
        # Check if the message contains the "Body" key
        if "Body" not in message:
            raise KeyError(f"No 'Body' key found in the message: {messageID}")

        # Extract the message's content and deserialize it into JSON
        msg_body = json.loads(message["Body"])

        # Check if the "Message" key is present in the message body
        # message["Body"]["Message"] is the "json.dumps(data)" defined in SNS.Client.publish(): Message = json.dumps({"default": json.dumps(data)})
        # See Fanout SNS to SQS: https://docs.aws.amazon.com/sns/latest/dg/sns-sqs-as-subscriber.html
        # Message Format Example: https://docs.aws.amazon.com/sns/latest/dg/sns-large-payload-raw-message-delivery.html
        if "Message" not in msg_body:
            raise KeyError(f"No 'Message' key found in the message body of message: {messageID}")

        # Extract the "Message" key and parse the message
        data = json.loads(msg_body["Message"])

        # Check for required keys in the data
        required_keys = ["job_id", "user_id", "s3_inputs_bucket", "s3_key_input_file", "input_file_name", "submit_time", "job_status"]
        missing_keys = [key for key in required_keys if key not in data]

        if missing_keys:
            raise ValueError(f"Missing required key-value pair in the data of message: {messageID}: {', '.join(missing_keys)}")

    # Subclass of ValueError
    # json.JSONDecodeError: https://docs.python.org/3/library/json.html#json.JSONDecodeError
    except json.JSONDecodeError as e:
        return jsonify({
            "code": 500,
            "status": "error",
            "message": f"Error deserializing JSON for message: {messageID}: {str(e)}"
        }), 500

    except KeyError as e:
        return jsonify({
            "code": 500,
            "status": "error",
            "message": f"Key error: {str(e)}"
        }), 500

    except ValueError as e:
        return jsonify({
            "code": 500,
            "status": "error",
            "message": f"Value error: {str(e)}"
        }), 500

    # Handle any other unexpected errors
    except Exception as e:
        return jsonify({
            "code": 500,
            "status": "error",
            "message": f"Unexpected error when processing message {messageID}: {str(e)}"
        }), 500




    # Extract job parameters from a message data
    try:
        job_id = data.get("job_id").get("S")
        user_id = data.get("user_id").get("S")
        inputs_bucket_name = data.get("s3_inputs_bucket").get("S")
        s3_key = data.get("s3_key_input_file").get("S")
        input_file_name = data.get("input_file_name").get("S")
        submit_time = data.get("submit_time").get("N")
        job_status = data.get("job_status").get("S")

    # Catch the error when post_body changed the data structure
    # Example: https://www.geeksforgeeks.org/python-attributeerror/
    except AttributeError as e:
        return jsonify({
            "code": 400,
            "status": "error",
            "message": f"Invalid data structure in the POST body for message {messageID}: {str(e)}"
        }), 400




    # Create an user directory under ANN_JOBS_DIR and return the path for target file
    # Create ~/gas/ann/jobs/<user_id>/ and return the target file path on the instance for downloading
    try:
        # os.path.basename(): https://docs.python.org/3/library/os.path.html#os.path.basename
        file_name = os.path.basename(s3_key) # uuid~test.vcf

        # os.path.dirname(): https://docs.python.org/3/library/os.path.html#os.path.dirname
        file_prefix = os.path.dirname(s3_key) # jycchien/user_id
        
        username_prefix = os.path.basename(file_prefix) # authenticated user id

        # os.path.join(): https://docs.python.org/3/library/os.path.html#os.path.join
        target_file_path = os.path.join(ANN_JOBS_DIR, username_prefix, file_name)

        # Get ~/gas/ann/jobs/<user_id>/
        user_dir = os.path.dirname(target_file_path)

        # Create job directory for a given user
        # os.path.exist(): https://docs.python.org/3/library/os.path.html#os.path.exists
        if not os.path.exists(user_dir):
            os.makedirs(user_dir, exist_ok = True)

    # Catch filesystem errors when manipulating non-existent files/directories
    # If failed to create an user directory, simply skip this input file, leaving it in the SQS
    except OSError as e:
        return jsonify({
            "code": 500,
            "status": "error",
            "message": f"Filesystem error - Failed to create an user directory for the scheduled job: {str(e)}"
        }), 500

    except Exception as e:
        return jsonify({
            "code": 500,
            "status": "error",
            "message": f"Unexpected error - Failed to create an user directory for the scheduled job: {str(e)}"
        }), 500




    # Get the input file S3 object and copy it to a local file
    try:
        # Download the input file from S3
        # S3.Client.download_file: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/download_file.html
        s3.download_file(inputs_bucket_name, s3_key, target_file_path)

    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code == "NoSuchBucket":
            return jsonify({
                "code": 404,
                "status": "error",
                "message": f"{err_code}: The specified bucket {inputs_bucket_name} does not exist - {msg}"
            }), 404

        # S3 Error Code Overview: https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html
        elif err_code == "NoSuchKey":
            return jsonify({
                "code": 404,
                "status": "error",
                "message": f"{err_code}: Object Key {s3_key} not found in the bucket {inputs_bucket_name} when downloading input files from S3 - {msg}"
            }), 404

        else:
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"Unexpected error: {err_code}, failed to download S3 file object - {msg}"
            }), 500

    # Check if the downloaded file can be found on the instance
    if not os.path.exists(target_file_path):
        return jsonify({
            "code": 404,
            "status": "error",
            "message": "Input file not found on the Anntools instance"
        }), 404




    # Update job status into "RUNNING" only if the previous status is "PENDING"
    try:
        # Update the “job_status” key in the annotations table in DynamoDB to “RUNNING”
        dynamo.update_item(

              TableName = ANNOTATIONS_TABLE,

              # The primary key of the item to be updated
              Key = {"job_id": {"S": job_id}},

              # An expression that defines one or more attributes to be updated, the action to be performed on them, and new values for them
              UpdateExpression = "SET job_status = :js",

              # A condition that must be satisfied in order for a conditional update to succeed
              ConditionExpression = "job_status = :cond",

              # One or more values that can be substituted in an expression
              ExpressionAttributeValues = {
                    ":js": {"S": "RUNNING"},
                    ":cond": {"S": "PENDING"}
              }
        )

    # Error handling with DynamoDB: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Programming.Errors.html
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code == "ConditionalCheckFailedException":
            return jsonify({
                "code": 400,
                "status": "error",
                "message": f"{err_code}: Conditional check failed for job {job_id} - {msg}"
            }), 400

        elif err_code in ("ProvisionedThroughputExceededException", "ThrottlingException"):
            return jsonify({
                "code": 400,
                "status": "error",
                "message": f"{err_code}: Rate of requests exceeds the allowed throughput for job {job_id} - {msg}"
            }), 400

        elif err_code == "ResourceNotFoundException":
            return jsonify({
                "code": 404,
                "status": "error",
                "message": f"{err_code}: DynamoDB Table or item with {job_id} not found - {msg}"
            }), 404

        elif err_code == "InternalServerError":
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"{err_code}: Failed to update item in DynamoDB Table - {msg}"
            }), 500

        else:
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"{err_code}: Failed to update item in DynamoDB Table - {msg}"
            }), 500




    # Run annotator at ~/anntools/jobs/<user>/uuid~filename
    # Catch the failure to launch the annotator job
    try:
        # Spawn a background subprocess to run the run.py annotator against ~/anntools/jobs/userX/uuid~filename
        # NOTE: the spawned subprocess runs independently from other spwaned subprocesses (concurrently)
        # subprocess.Popen(): https://docs.python.org/3/library/subprocess.html#subprocess.Popen
        # DataCamp Python Subprocess: https://www.datacamp.com/tutorial/python-subprocess
        command = ["python", os.path.join(ANN_DIR, "run.py"), target_file_path]
        process = subprocess.Popen(command, cwd = user_dir)

        # Check if run.py is exited with return code = 1 (sys.exit(1))
        # Even if run.py encounters an error, the annotator can assume all is fine and delete the message from the queue
        # Based on Vas's saying on Ed: https://edstem.org/us/courses/68475/discussion/5652392
        if process.returncode == 1:
            print(f"Annotator detects that an error occurs in run.py when processing {file_name}")
            # Not returning True

    # Catch the attempt to execute non-existent files/directories
    # subprocess Exceptions: https://docs.python.org/3/library/subprocess.html#exceptions
    except OSError as e:
        return jsonify({
            "code": 500,
            "status": "error",
            "message": f"Annotator file or output directory does not exist: {str(e)}"
        }), 500

    # Catch other unexpected errors derived from subprocess
    # subprocess.SubprocessError: https://docs.python.org/3/library/subprocess.html#subprocess.SubprocessError
    except subprocess.SubprocessError as e:
        return jsonify({
            "code": 500,
            "status": "error",
            "message": f"Failed to launch annotator job: {str(e)}"
        }), 500




    # Delete message from queue, if job was successfully submitted
    try:

        print(f"Start deleting message: {messageID}")

        # SQS.Client.delete_message(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs/client/delete_message.html
        sqs.delete_message(

            QueueUrl = SQS_QUEUE_URL_REQUESTS,

            # ReceiptHandle is an identifier associated with the act of receiving the message
            # If receiving a message more than once, the ReceiptHandle is different each time when receiving that message
            # See SQS.Client.receive_message(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs/client/receive_message.html

            # The receipt handle allows the later instance to successfully delete the message after processing, despite the previous instance's failure
            # This mechanism ensures that messages are properly handled and removed from the queue only when successfully processed, preventing duplicate processing and maintaining the queue's integrity.
            # See SQS Developer Guide @ "Receipt Handle": https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-queue-message-identifiers.html
            ReceiptHandle = message["ReceiptHandle"]

        )

    # Not catching "QueueDoesNotExist" since we have caught that before (@receive_message())
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code == "ReceiptHandleIsInvalid":
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"{err_code}: Failed to delete message {messageID} due to invalild receipt handle - {msg}"
            }), 500

        elif err_code == "InvalidIdFormat":
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"{err_code}: Failed to delete message {messageID} due to the specified receipt handle isn’t valid for the current version - {msg}"
            }), 500

        elif err_code == "RequestThrottled":
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"{err_code}: Rate of requests exceeds the allowed throughput for message {messageID} - {msg}"
            }), 500

        else:
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"{err_code}: Failed to delete message {messageID} - {msg}"
            }), 500

    print(f"message: {messageID} is deleted")
    print(f"Process for message: {messageID} is completed")


@app.route("/", methods=["GET"])
def annotator_webhook():

    return ("Annotator webhook; POST job to /process-job-request"), 200


"""
A13 - Replace polling with webhook in annotator

Receives request from SNS; queries job queue and processes message.
Reads request messages from SQS and runs AnnTools as a subprocess.
Updates the annotations database with the status of the request.
"""


@app.route("/process-job-request", methods=["POST"])
def annotate():

    # Check message type
    # Check if it's a subscription confirmation request
    # If it is, to confirm the subscription, must visit the SubscribeURL included in the payload of the request body
    # Subscribing an HTTPS endpoint to an Amazon SNS topic: https://docs.aws.amazon.com/sns/latest/dg/SendMessageToHttp.prepare.html
    # Parsing Amazon SNS message formats: https://docs.aws.amazon.com/sns/latest/dg/sns-message-and-json-formats.html
    # Example: https://gist.github.com/iMilnb/bf27da3f38272a76c801
    if request.headers.get("x-amz-sns-message-type") == "SubscriptionConfirmation":

        confirmation_url = json.loads(request.data)["SubscribeURL"]

        # Visit the confirmation URL to confirm SNS topic subscription
        requests.get(confirmation_url)
        return jsonify({
            "code": 200,
            "status": "success",
            "message": "Subscription confirmed"
        }), 200

    try:
        # SQS.Client.receive_message(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs/client/receive_message.html
        response = sqs.receive_message(

            # The URL of the Amazon SQS queue from which messages are received (case sensitive)
            QueueUrl = SQS_QUEUE_URL_REQUESTS,

            # Retrieve messages up to the receive maximum
            MaxNumberOfMessages = MAX_MESSAGES,

            # The duration /sec for which receive_message() waits for a message to arrive in the SQS queue before returning
            WaitTimeSeconds = WAIT_TIME # Long polling

        )

    # SQS Client Exceptions: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/services/sqs.html#client-exceptions
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        # We want to raise this error since it's critical
        if err_code == "QueueDoesNotExist":
            return jsonify({
                "code": 404,
                "status": "error",
                "message": f"The specified SQS queue does not exist - {msg}"
            }), 404

        elif err_code == "OverLimit":
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"{err_code}: The maximum number of in flight messages is reached - {msg}"
            }), 500

        elif err_code == "RequestThrottled":
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"{err_code}: Rate of requests exceeds the allowed throughput - {msg}"
            }), 500

        else:
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"{err_code}: Failed to receive messages from SQS queue - {msg}"
            }), 500

    messages = response.get("Messages")

    # Check if we are getting empty messages
    if not messages:
        print("No messages received from the queue")

    else:
        print(f"Received {len(messages)} messages from SQS queue")

        for message in messages:
            process_message(message)


    return (
        jsonify(
            {
                "code": 201,
                "message": "Annotation job request processed."
            }
        ),
        201,
    )

### EOF