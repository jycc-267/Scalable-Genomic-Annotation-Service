# thaw_app_config.py
#
# Set app configuration options for thaw utility
#
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

    # AWS DynamoDB table
    AWS_DYNAMODB_ANNOTATIONS_TABLE = f"{iam_username}_annotations"
    AWS_DYNAMODB_ANNOTATIONS_USER_ID_INDEX = "user_id_index"

    # AWS Glacier
    AWS_GLACIER_VAULT = "ucmpcs"

    # AWS SNS topics
    AWS_SNS_ARN_RESTORE = f"arn:aws:sns:us-east-1:127134666975:{iam_username}_a17_results_restore"

    # AWS SQS queues
    AWS_SQS_WAIT_TIME = 20
    AWS_SQS_MAX_MESSAGES = 10
    QUEUE_NAME_THAW = f"{iam_username}_a17_results_thaw"
    SQS_QUEUE_URL_THAW = f"https://sqs.us-east-1.amazonaws.com/127134666975/{iam_username}_a17_results_thaw"

    # User Account DB
    ACCOUNT_DB = f"{iam_username}_accounts"

### EOF