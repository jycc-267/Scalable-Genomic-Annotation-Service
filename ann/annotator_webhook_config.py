# ann_config.py
#
# Set GAS annotator configuration options
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

# ALL UPPERCASE!!!: https://github.com/pallets/flask/issues/881
class Config(object):

    CSRF_ENABLED = True

    ANN_DIR = "/home/ubuntu/gas/ann"
    ANN_JOBS_DIR = f"{ANN_DIR}/jobs"
    LOGFILE_SUFFIX = ".vcf.count.log"
    ANNOTFILE_SUFFIX = ".annot.vcf"

    AWS_REGION_NAME = (
        os.environ["AWS_REGION_NAME"]
        if ("AWS_REGION_NAME" in os.environ)
        else "us-east-1"
    )

    CNET_ID = {iam_username}
    COMPLETE_TIME = 0

    # AWS S3 upload parameters
    AWS_S3_KEY_PREFIX = f"{iam_username}/"
    AWS_S3_INPUTS_BUCKET = "gas-inputs"
    AWS_S3_RESULTS_BUCKET = "gas-results"

    # AWS SNS topics"
    TOPIC_NAME_REQUESTS = f"{iam_username}_a17_job_requests"
    TOPIC_NAME_RESULTS = f"{iam_username}_a17_job_results"
    TOPIC_ARN_REQUESTS = f"arn:aws:sns:us-east-1:127134666975:{iam_username}_a17_job_requests"
    TOPIC_ARN_RESULTS = f"arn:aws:sns:us-east-1:127134666975:{iam_username}_a17_job_results"

    # AWS SQS queues
    AWS_SQS_WAIT_TIME = 20
    AWS_SQS_MAX_MESSAGES = 10
    QUEUE_NAME_REQUESTS = f"{iam_username}_a17_job_requests"
    QUEUE_NAME_RESULTS = f"{iam_username}_a17_job_results"
    SQS_QUEUE_URL_REQUESTS = f"https://sqs.us-east-1.amazonaws.com/127134666975/{iam_username}_a17_job_requests"
    SQS_QUEUE_URL_RESULTS = f"https://sqs.us-east-1.amazonaws.com/127134666975/{iam_username}_a17_job_results"

    # AWS DynamoDB
    AWS_DYNAMODB_ANNOTATIONS_TABLE = f"{iam_username}_annotations"


### EOF
