# thaw_app.py
#
# Thaws upgraded (Premium) user data
##

import boto3
import json
import requests
import os
import sys
import time

from botocore.exceptions import ClientError
from flask import Flask, request, jsonify

# Import utility helpers
sys.path.insert(1, os.path.realpath(os.path.pardir))
import helpers

app = Flask(__name__)
app.url_map.strict_slashes = False

# Get configuration and add to Flask app object
environment = "thaw_app_config.Config"
app.config.from_object(environment)

REGION = app.config["AWS_REGION_NAME"]
MAX_MESSAGES = app.config["AWS_SQS_MAX_MESSAGES"]
WAIT_TIME = app.config["AWS_SQS_WAIT_TIME"]
SQS_QUEUE_URL_THAW = app.config["SQS_QUEUE_URL_THAW"]
AWS_DYNAMODB_ANNOTATIONS_TABLE = app.config["AWS_DYNAMODB_ANNOTATIONS_TABLE"]
ACCOUNT_DB = app.config["ACCOUNT_DB"]
USER_ID_INDEX = app.config["AWS_DYNAMODB_ANNOTATIONS_USER_ID_INDEX"]
AWS_GLACIER_VAULT = app.config["AWS_GLACIER_VAULT"]
AWS_SNS_ARN_RESTORE = app.config["AWS_SNS_ARN_RESTORE"]

sqs = boto3.client("sqs", region_name = REGION)
glacier = boto3.client("glacier", region_name = REGION)
dynamo = boto3.client("dynamodb", region_name = REGION)

@app.route("/", methods=["GET"])
def home():
    return f"This is the Thaw utility: POST requests to /thaw."


@app.route("/thaw", methods=["POST"])
def thaw_premium_user_data():

    # Check if it's a subscription confirmation request
    # If it is, to confirm the subscription, must visit the SubscribeURL included in the payload of the request body
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
            QueueUrl = SQS_QUEUE_URL_THAW,
            MaxNumberOfMessages = MAX_MESSAGES,
            WaitTimeSeconds = WAIT_TIME # Long polling
        )

    # SQS Client Exceptions: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/services/sqs.html#client-exceptions
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code == "QueueDoesNotExist":
            return jsonify({
                "code": 404,
                "status": "error",
                "message": f"The specified SQS queue does not exist - {msg}"
            }), 404
        else:
            return jsonify({
                "code": 500,
                "status": "error",
                "message": f"{err_code}: Failed to receive messages from SQS queue - {msg}"
            }), 500

    messages = response.get("Messages")

    # Check if we are getting empty messages
    if not messages:
        print("No thawing messages received from the queue")

    else:
        print(f"Received {len(messages)} thawing messages from SQS queue")

        for message in messages:
            messageID = message.get("MessageId")
            print(f"Start processing thawing message: {messageID}")

            try:
                # Check if the message contains the "Body" key
                if "Body" not in message:
                    raise KeyError(f"No 'Body' key found in the message: {messageID}")

                # Extract the message's content and deserialize it into JSON
                msg_body = json.loads(message["Body"])

                # Check if the "Message" key is present in the message body
                # message["Body"]["Message"] is the "json.dumps(data)" defined in SNS.Client.publish(): Message = json.dumps({"default": json.dumps(data)})
                if "Message" not in msg_body:
                    raise KeyError(f"No 'Message' key found in the message body of message: {messageID}")

                data = json.loads(msg_body["Message"])

                if "user_id" not in data:
                    raise ValueError(f"Missing user_id in the data of message: {messageID}")

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

            # Extract user_id from a message data, we want to get all archives belonging to a current premium_user (suggesting that this user was free_user)
            user_id = data["user_id"]
            user_profile = helpers.get_user_profile(id = user_id, db_name = ACCOUNT_DB)

            if user_profile.get("role") == "premium_user":
                try: 
                    # DynamoDB.Client.query(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb/client/query.html
                    # Response example: https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_Query.html#API_Query_Examples
                    archives = dynamo.query(
                        TableName = AWS_DYNAMODB_ANNOTATIONS_TABLE,
                        IndexName = USER_ID_INDEX,
                        Select = "SPECIFIC_ATTRIBUTES",
                        ProjectionExpression = "job_id, results_file_archive_id",
                        KeyConditionExpression = "user_id = :uid",
                        ExpressionAttributeValues = {
                            ":uid": {"S": user_id}},
                        # Syntax for filter expressions: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.OperatorsAndFunctions.html#Expressions.OperatorsAndFunctions.Syntax
                        FilterExpression = "attribute_exists(results_file_archive_id)"
                    )
                except ClientError as e:
                    msg = e.response["Error"]["Message"]
                    err_code = e.response["Error"]["Code"]

                    if err_code == "ResourceNotFoundException":
                        return jsonify({
                            "code": 404,
                            "status": "error",
                            "message": f"{err_code}: Table or index not found - {msg}"
                        }), 404
                    elif err_code == "InternalServerError":
                        return jsonify({
                            "code": 500,
                            "status": "error",
                            "message": f"{err_code}: Failed to query item in annotation table - {msg}"
                        }), 500
                    else:
                        return jsonify({
                            "code": 500,
                            "status": "error",
                            "message": f"Unexpected ClientError: DynamoDB query failed - {str(e)}"
                        }), 500

                # Get archive_id for every archive and initiate the "thawing" job
                for archive in archives.get("Items", []):
                    archive_id = archive["results_file_archive_id"]["S"]
                    annotation_job_id = archive["job_id"]["S"]
                    initiate_job(annotation_job_id = annotation_job_id, archive_id = archive_id)

            try:
                print(f"Start deleting message in results_thaw queue: {messageID}")

                # SQS.Client.delete_message(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs/client/delete_message.html
                sqs.delete_message(
                    QueueUrl = SQS_QUEUE_URL_THAW,
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
                else:
                    return jsonify({
                            "code": 500,
                            "status": "error",
                            "message": f"{err_code}: Failed to delete message {messageID} - {msg}"
                        }), 500

            print(f"Thawing message: {messageID} is deleted")
            print(f"Process for thawing message: {messageID} is completed")

    return (
        jsonify(
            {
                "code": 201,
                "message": "Thawing job request processed."
            }
        ),
        201
    )


def initiate_job(annotation_job_id, archive_id, tier = "Expedited"): 
    try: 
        # Initiate job to retrieve archive and thaw it from glacier
        # Glacier.Client.initiate_job(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/glacier.html#Glacier.Client.initiate_job
        response = glacier.initiate_job(
            vaultName = AWS_GLACIER_VAULT, 
            jobParameters = {

                "Type": "archive-retrieval",
                "ArchiveId": archive_id, 

                # The SNS topic ARN to which Glacier sends a notification when the job is completed and the output is ready to be downloaded
                "SNSTopic": AWS_SNS_ARN_RESTORE, 

                # The tier to use for a select or an archive retrieval job
                "Tier": tier,

                # Hold the annotation_job_id
                "Description": annotation_job_id
            }
        )
        print(f"Initiate a {tier} archive-retrieval (thawing) job for archive: {archive_id}")
    except glacier.exceptions.InsufficientCapacityException: 
        initiate_job(annotation_job_id = annotation_job_id, archive_id = archive_id, tier = "Standard")




### EOF