# notify.py
#
# Notify user of job completion via email
##

import boto3
import json
import os
import sys
import time
from datetime import datetime

import pytz

from botocore.exceptions import ClientError

# Import utility helpers
sys.path.insert(1, os.path.realpath(os.path.pardir))
import helpers

# Get configuration
from configparser import ConfigParser, ExtendedInterpolation

config = ConfigParser(os.environ, interpolation=ExtendedInterpolation())
config.read("../util_config.ini")
config.read("notify_config.ini")

"""A12
Reads result messages from SQS and sends notification emails.
"""

MAX_MESSAGES = int(config["sqs"]["MaxMessages"])
WAIT_TIME = int(config["sqs"]["WaitTime"])
SQS_QUEUE_URL_Results = config["sqs"]["SQS_QUEUE_URL_Results"]

def format_cdt_time(epoch_time):

    # Convert epoch time to datetime in UTC
    utc_time = datetime.fromtimestamp(int(epoch_time), tz = pytz.UTC)

    # Convert to Central Time
    cdt_tz = pytz.timezone("America/Chicago")
    cdt_time = utc_time.astimezone(cdt_tz)

    # Format time as YYYY-MM-DD HH:MM
    return cdt_time.strftime("%Y-%m-%d %H:%M")



def handle_results_queue(sqs = None):

    if sqs is None:
        sqs = boto3.client("sqs", region_name = config["aws"]["AwsRegionName"])

    while True:
        # Read messages from the queue
        try:
            # Check if our target queue exists or not
            # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs/client/get_queue_url.html
# VARY BY ASSIGNMENT
            if not sqs.get_queue_url(QueueName = config["sqs"]["QueueName_Results"]).get("QueueUrl"):
                raise ClientError

            # SQS.Client.receive_message(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs/client/receive_message.html
            response = sqs.receive_message(

                # The URL of the Amazon SQS queue from which messages are received (case sensitive)
                QueueUrl = SQS_QUEUE_URL_Results,

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
                print(f"{err_code}: The Completed Jobs SQS queue does not exist - {msg}")
                raise e

            elif err_code == "OverLimit":
                print(f"{err_code}: The maximum number of in flight messages is reached - {msg}")
                continue

            elif err_code == "RequestThrottled":
                print(f"{err_code}: Rate of requests exceeds the allowed throughput - {msg}")
                continue

            else:
                print(f"{err_code}: Failed to receive messages from Completed Jobs SQS queue  - {msg}")
                continue

        messages = response.get("Messages")

        # Check if we are getting empty messages
        if not messages:
            print("No messages received from the job results queue")
            continue

        else:
            print(f"Received {len(messages)} messages from SQS queue")

            # process_message() returns True if catching an error when processing a message
            for message in messages:

                messageID = message.get('MessageId')
                print(f"Start processing message: {messageID}")

                # Extract job parameters from message body
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
                    required_keys = ["job_id", "user_id", "complete_time"]
                    missing_keys = [key for key in required_keys if key not in data]

                    if missing_keys:
                        raise ValueError(f"Missing required key-value pair in the data of message: {messageID}: {', '.join(missing_keys)}")

                # Subclass of ValueError
                # json.JSONDecodeError: https://docs.python.org/3/library/json.html#json.JSONDecodeError
                except json.JSONDecodeError as e:
                    print(f"Error deserializing JSON for message: {messageID}: {str(e)}")
                    continue

                except KeyError as e:
                    print(f"Key error: {str(e)}")
                    continue

                except ValueError as e:
                    print(f"Value error: {str(e)}")
                    continue

                # Handle any other unexpected errors
                except Exception as e:
                    print(f"Unexpected error when processing message {messageID}: {str(e)}")
                    continue

                # Get user profile
                try: 
                    user_profile = helpers.get_user_profile(id = data["user_id"], db_name = config["gas"]["AccountsDatabase"])
                    user_email = user_profile["email"]
                except ClientError as e:
                    msg = e.response["Error"]["Message"]
                    err_code = e.response["Error"]["Code"]
                    print(f"{err_code}: Failed to get email/user's profile - {msg}")
                    continue
                except Exception as e:
                    print(f"Failed to get email/user's profile - {e}")
                    continue

                # Format completion time (epoch) in CDT
                cdt_time = format_cdt_time(data["complete_time"])

# VARY BY ASSIGNMENT: endpoint JobDetails_URL
                # Prepare email content
                job_id = data["job_id"]

                email_body = (f"Annotation job completed at {cdt_time}.\n\n"
                              f"Click here to view results: {config['gas']['JobDetails_URL']}{job_id}")

                try:
                    # Process messages --> send email to user
                    helpers.send_email_ses(
                        recipients = user_email,
                        sender = f"{config['gas']['SenderEmail']}",
                        subject = f"Results available for job {job_id}",
                        body = email_body
                    )

                    # Delete message after successful email sending
                    sqs.delete_message(
                        QueueUrl = SQS_QUEUE_URL_Results,
                        ReceiptHandle = message["ReceiptHandle"]
                    )

                # Not catching "QueueDoesNotExist" since we have caught that before (@receive_message())
                except ClientError as e:
                    msg = e.response["Error"]["Message"]
                    err_code = e.response["Error"]["Code"]

                    if err_code == "ReceiptHandleIsInvalid":
                        print(f"{err_code}: Failed to delete message {messageID} due to invalild receipt handle - {msg}")
                        continue

                    elif err_code == "InvalidIdFormat":
                        print(f"{err_code}: Failed to delete the message {messageID} due to the specified receipt handle isn’t valid for the current version - {msg}")
                        continue

                    elif err_code == "RequestThrottled":
                        print(f"{err_code}: Rate of requests exceeds the allowed throughput for message {messageID} - {msg}")
                        continue

                    else:
                        print(f"Failed to delete the message {messageID}")
                        continue

                print(f"message: {messageID} is deleted")
                print(f"Process for message: {messageID} is completed")


def main():
    handle_results_queue()


if __name__ == "__main__":
    main()

### EOF