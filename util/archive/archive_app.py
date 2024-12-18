# archive_app.py
#
# Archive Free user data
##


import boto3
import json
import requests
import sys
import time
import os

from flask import Flask, jsonify, request

# Import utility helpers
sys.path.insert(1, os.path.realpath(os.path.pardir))
import helpers

app = Flask(__name__)
app.url_map.strict_slashes = False

# Get configuration and add to Flask app object
environment = "archive_app_config.Config"
app.config.from_object(environment)

REGION = app.config["AWS_REGION_NAME"]
MAX_MESSAGES = app.config["AWS_SQS_MAX_MESSAGES"]
WAIT_TIME = app.config["AWS_SQS_WAIT_TIME"]
SQS_QUEUE_URL_ARCHIVE = app.config["SQS_QUEUE_URL_ARCHIVE"]
AWS_S3_RESULTS_BUCKET = app.config["AWS_S3_RESULTS_BUCKET"]
AWS_DYNAMODB_ANNOTATIONS_TABLE = app.config["AWS_DYNAMODB_ANNOTATIONS_TABLE"]

sqs = boto3.client("sqs", region_name = REGION)
s3 = boto3.client("s3", region_name = REGION)
glacier = boto3.client("glacier", region_name = REGION)
dynamo = boto3.client("dynamodb", region_name = REGION)


@app.route("/", methods=["GET"])
def home():
    return f"This is the Archive utility: POST requests to /archive."


@app.route("/archive", methods=["POST"])
def archive_free_user_data():

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
            QueueUrl = SQS_QUEUE_URL_ARCHIVE,

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
        print("No archive messages received from the queue")

    else:
        print(f"Received {len(messages)} archive messages from SQS queue")

        for message in messages:

            messageID = message.get("MessageId")
            print(f"Start processing archive message: {messageID}")

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
                required_keys = ["job_id", "user_id", "complete_time", "s3_key_result_file", "s3_key_log_file"]
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
                job_id = data.get("job_id")
                user_id = data.get("user_id")
                complete_time = data.get("complete_time")
                s3_key_result_file = data.get("s3_key_result_file")
                s3_key_log_file = data.get("s3_key_log_file")

            # Catch the error when post_body changed the data structure
            # Example: https://www.geeksforgeeks.org/python-attributeerror/
            except AttributeError as e:
                return jsonify({
                    "code": 400,
                    "status": "error",
                    "message": f"Invalid data structure in the data for message {messageID}: {str(e)}"
                }), 400


            user_profile = helpers.get_user_profile(id = user_id, db_name = app.config["ACCOUNT_DB"])
            print(user_profile.keys())
            print(f"user role: {user_profile.get('role')}")


            if user_profile.get("role") == "free_user":
                try:
                    # Get the result file from S3
                    # S3.Client.get_object(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/get_object.html
                    result_file = s3.get_object(
                        Bucket = AWS_S3_RESULTS_BUCKET,
                        Key = s3_key_result_file
                    )

                except ClientError as e:
                    msg = e.response["Error"]["Message"]
                    err_code = e.response["Error"]["Code"]

                    if err_code in ("NoSuchKey", "NoSuchBucket"):
                        return jsonify({
                            "code": 404,
                            "status": "error",
                            "message": f"{err_code}: Bucket or File not found in S3 - {msg}"
                        }), 404
                    else:
                        return jsonify({
                            "code": 500,
                            "status": "error",
                            "message": f"Unexpected ClientError: S3 error getting log file content - {msg}"
                        }), 500
                except Exception as e:
                        return jsonify({
                            "code": 500,
                            "status": "error",
                            "message": f"Unexpected ClientError: S3 error getting log file content - {str(e)}"
                        }), 500




                try:
                    # Object data result_file["Body"] is StreamingBody, need to read it into bytes for archiving
                    # botocore.response.StreamingBody: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/response.html#botocore.response.StreamingBody
                    result_file_content = result_file["Body"].read()

                    # Upload archive to Glacier
                    # Glacier.Client.upload_archive(): https://boto3.amazonaws.com/v1/documentation/api/1.35.6/reference/services/glacier/client/upload_archive.html
                    archive_response  = glacier.upload_archive(

                        # The name of the vault
                        vaultName = app.config["AWS_GLACIER_VAULT"],

                        # The data bytes to upload as an archive
                        body = result_file_content
                    )

                except ClientError as e:
                    msg = e.response["Error"]["Message"]
                    err_code = e.response["Error"]["Code"]

                    if err_code == "ResourceNotFound":
                        return jsonify({
                            "code": 404,
                            "status": "error",
                            "message": f"{err_code}: The specified vault does not exist - {msg}"
                        }), 404
                    elif err_code in ("InvalidParameterValue", "MissingParameterValue"):
                        return jsonify({
                            "code": 500,
                            "status": "error",
                            "message": f"{err_code}: Invalid/Missing parameter value - {msg}"
                        }), 500
                    else:
                        return jsonify({
                            "code": 500,
                            "status": "error",
                            "message": f"Unexpected ClientError: Failed to upload archive to Glacier - {msg}"
                        }), 500
                except Exception as e:
                        return jsonify({
                            "code": 500,
                            "status": "error",
                            "message": f"Unexpected ClientError: S3 error getting log file content - {str(e)}"
                        }), 500




                try:
                    # Add Glacier object ID to the annotation table in DynamoDB
                    dynamo.update_item(
                        TableName = AWS_DYNAMODB_ANNOTATIONS_TABLE,
                        Key = {"job_id": {"S": job_id}},
                        UpdateExpression = "SET results_file_archive_id = :archiveId",
                        ExpressionAttributeValues = {':archiveId': {"S": archive_response["archiveId"]}}
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




                try: 
                    # Delete the file from S3 result bucket
                    # S3.Client.delete_object(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/delete_object.html
                    s3.delete_object(
                        Bucket = AWS_S3_RESULTS_BUCKET, 
                        Key = s3_key_result_file
                    )
                except ClientError as e:
                    msg = e.response["Error"]["Message"]
                    err_code = e.response["Error"]["Code"]

                    if err_code in ("NoSuchKey", "NoSuchBucket"):
                        return jsonify({
                            "code": 404,
                            "status": "error",
                            "message": f"{err_code}: Bucket or File not found in S3 - {msg}"
                        }), 404
                    else:
                        return jsonify({
                            "code": 500,
                            "status": "error",
                            "message": f"Unexpected ClientError: S3 error getting log file content - {msg}"
                        }), 500
                except Exception as e:
                        return jsonify({
                            "code": 500,
                            "status": "error",
                            "message": f"Unexpected ClientError: S3 error getting log file content - {str(e)}"
                        }), 500




            # Delete message from queue, if job was successfully submitted
            try:
                print(f"Start deleting message in archive queue: {messageID}")

                # SQS.Client.delete_message(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs/client/delete_message.html
                sqs.delete_message(

                    QueueUrl = SQS_QUEUE_URL_ARCHIVE,

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

            print(f"Archive message: {messageID} is deleted")
            print(f"Process for archive message: {messageID} is completed")

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