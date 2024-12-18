# archive_app_config.py
#
# Set app configuration options for archive utility
##

import os

# Get the IAM username that was stashed at launch time
try:
    with open("/home/ubuntu/.launch_user", "r") as file:
        iam_username = file.read().replace("\n", "")
except FileNotFoundError as e:
    if "LAUNCH_USER" in os.environ:
        iam_username = os.environ["LAUNCH_USER"]
    else:
        # Unable to set username, so exit
        print("Unable to find launch user name in local file or environment!")
        raise e


class Config(object):

    CSRF_ENABLED = True

    AWS_REGION_NAME = "us-east-1"

    CNET_ID = {iam_username}

    # AWS Glacier Vault Name
    AWS_GLACIER_VAULT = "ucmpcs"

    # AWS S3 upload parameters
    AWS_S3_KEY_PREFIX = f"{iam_username}/"
    AWS_S3_INPUTS_BUCKET = "gas-inputs"
    AWS_S3_RESULTS_BUCKET = "gas-results"

    # AWS SNS topics"
    TOPIC_NAME_ARCHIVE = f"{iam_username}_a17_results_archive"
    TOPIC_ARN_ARCHIVE = f"arn:aws:sns:us-east-1:127134666975:{iam_username}_a17_results_archive"

    # AWS SQS queues
    AWS_SQS_WAIT_TIME = 20
    AWS_SQS_MAX_MESSAGES = 10
    QUEUE_NAME_ARCHIVE = f"{iam_username}_a17_results_archive"
    SQS_QUEUE_URL_ARCHIVE = f"https://sqs.us-east-1.amazonaws.com/127134666975/{iam_username}_a17_results_archive"

    # AWS DynamoDB table
    AWS_DYNAMODB_ANNOTATIONS_TABLE = f"{iam_username}_annotations"

    # User Account DB
    ACCOUNT_DB = f"{iam_username}_accounts"

### EOF