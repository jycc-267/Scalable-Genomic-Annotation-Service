# restore.py
#
# The Lambda Function
#
# Restores thawed data, saving objects to S3 results bucket
# NOTE: This code is for an AWS Lambda function
##

import boto3
import json
from botocore.exceptions import ClientError

# Define constants here; no config file is used for Lambdas
DYNAMODB_TABLE = "jycchien_annotations"
SQS_QUEUE_URL_RESTORE = "https://sqs.us-east-1.amazonaws.com/127134666975/jycchien_a17_results_restore"
AWS_GLACIER_VAULT = "ucmpcs"
S3_RESULT_BUCKET = "gas-results"
REGION = "us-east-1"
MAX_MESSAGES = 10
WAIT_TIME = 20

sqs = boto3.client("sqs", region_name = REGION)
glacier = boto3.client("glacier", region_name = REGION)
dynamo = boto3.client("dynamodb", region_name = REGION)
s3 = boto3.client("s3", region_name = REGION)


# Define Lambda function handler in Python: https://docs.aws.amazon.com/lambda/latest/dg/python-handler.html
# SNS to Lambda vs SNS to SQS to Lambda: https://stackoverflow.com/questions/42656485/sns-to-lambda-vs-sns-to-sqs-to-lambda
# Using Lambda with SQS: https://docs.aws.amazon.com/lambda/latest/dg/with-sqs-example.html
def lambda_handler(event, context):
    print("Received event: " + json.dumps(event, indent=2))

    try:
        # SQS.Client.receive_message(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs/client/receive_message.html
        response = sqs.receive_message(
            QueueUrl = SQS_QUEUE_URL_RESTORE,
            MaxNumberOfMessages = MAX_MESSAGES,
            WaitTimeSeconds = WAIT_TIME # Long polling
        )

    # SQS Client Exceptions: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/services/sqs.html#client-exceptions
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code == "QueueDoesNotExist":
            print(f"{err_code}: The specified SQS queue does not exist - {msg}")
        else:
            print(f"{err_code}: Failed to receive messages from SQS queue - {msg}")

    messages = response.get("Messages")

    # Check if we are getting empty messages
    if not messages:
        print("No archive messages received from the queue")

    else:
        print(f"Received {len(messages)} archive messages from SQS queue")

        for message in messages:
            messageID = message.get("MessageId")
            print(f"Start processing restoration message: {messageID}")

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

                # Check for required keys in the data
                required_keys = ["ArchiveId", "JobId", "JobDescription", "StatusCode"]
                missing_keys = [key for key in required_keys if key not in data]

                if missing_keys:
                    raise ValueError(f"Missing required key-value pair in the data of message: {messageID}: {', '.join(missing_keys)}")

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

            # SNS JSON body for a Glacier retrieval request: https://stackoverflow.com/questions/15283281/amazon-glacier-how-to-associate-an-archive-retrieval-sns-response-with-its-job
            if data["StatusCode"] != "Succeeded": 
                print(f"Archive-retrieval (thawing) job is not succeeded for message: {messageID}")
                continue

            JobId = data["JobId"]
            ArchiveId = data["ArchiveId"]
            annotation_job_id = data["JobDescription"]

            try:
                # Glacier.Client.get_job_output(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/glacier.html#Glacier.Client.get_job_output
                thawed_file = glacier.get_job_output(
                    vaultName = AWS_GLACIER_VAULT,
                    jobId = JobId
                )
            except ClientError as e:
                msg = e.response["Error"]["Message"]
                err_code = e.response["Error"]["Code"]

                if err_code == "ResourceNotFoundException":
                    print(f"{err_code}: The specified archive retrieval job does not exist - {msg}")
                    continue
                else:
                    print(f"{err_code}: Unexpected ClientError when getting thawed S3 file {annotation_job_id} - {msg}")
                    continue

            # thawed_file["body"] is StreamingBody, read it into bytes for uploading to S3 later on
            # botocore.response.StreamingBody: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/response.html#botocore.response.StreamingBody
            result_content = thawed_file["body"].read()

            try:
                # Search item by the partition key to get s3
                # DynamoDB.Client.get_item: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb/client/get_item.html
                item = dynamo.get_item(
                    TableName = DYNAMODB_TABLE,
                    Key = {"job_id": {"S": annotation_job_id}}
                )
            except ClientError as e:
                msg = e.response["Error"]["Message"]
                err_code = e.response["Error"]["Code"]

                if err_code == "ResourceNotFoundException":
                    print(f"{err_code}: Table or Job item not found - {msg}")
                    continue
                else:
                    print(f"Unexpected ClientError: DynamoDB query failed - {str(e)}")
                    continue

            if not item.get("Item"):
                print(f"Annotation Job {annotation_job_id} not found in the dynamodb API response")
                continue

            ann_job = item.get("Item")
            s3_key_result_file = ann_job.get("s3_key_result_file").get("S")

            try:
                # S3.Client.put_object(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/put_object.html
                s3_response = s3.put_object(
                    Body = result_content,
                    Bucket = S3_RESULT_BUCKET,
                    Key = s3_key_result_file
                )
            except ClientError as e:
                msg = e.response["Error"]["Message"]
                err_code = e.response["Error"]["Code"]
                print(f"{err_code}: S3 Upload Failed - {msg}")
                continue

            try:
                # Delete an archive from a vault
                # Glacier.Client.delete_archive(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/glacier/client/delete_archive.html
                glacier.delete_archive(
                    vaultName = AWS_GLACIER_VAULT,
                    archiveId = ArchiveId
                )
            except ClientError as e:
                msg = e.response["Error"]["Message"]
                err_code = e.response["Error"]["Code"]

                if err_code == "ResourceNotFoundException":
                    print(f"{err_code}: The specified archive does not exist - {msg}")
                    continue
                else:
                    print(f"{err_code}: Unexpected ClientError when deleting archive {ArchiveId} - {msg}")
                    continue

            try:
                # Add Glacier object ID to the annotation table in DynamoDB
                # DynamoDB.Client.update_item(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb/client/update_item.html
                dynamo.update_item(
                    TableName = DYNAMODB_TABLE,
                    Key = {"job_id": {"S": annotation_job_id}},
                    UpdateExpression = "REMOVE results_file_archive_id"
                )
            except ClientError as e:
                msg = e.response["Error"]["Message"]
                err_code = e.response["Error"]["Code"]

                if err_code == "ResourceNotFoundException":
                    print(f"{err_code}: Table or Job item not found - {msg}")
                    continue
                else:
                    print(f"{err_code}: Failed to update item in DynamoDB Table - {msg}")
                    continue

            # Delete message from queue
            try:
                print(f"Start deleting message in results_restore queue: {messageID}")

                # SQS.Client.delete_message(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sqs/client/delete_message.html
                sqs.delete_message(
                    QueueUrl = SQS_QUEUE_URL_RESTORE,
                    ReceiptHandle = message["ReceiptHandle"]
                )
            # Not catching "QueueDoesNotExist" since we have caught that before (@receive_message())
            except ClientError as e:
                msg = e.response["Error"]["Message"]
                err_code = e.response["Error"]["Code"]

                if err_code == "ReceiptHandleIsInvalid":
                    print(f"{err_code}: Failed to delete message {messageID} due to invalild receipt handle - {msg}")
                    continue
                else:
                    print(f"{err_code}: Failed to delete message {messageID} - {msg}")
                    continue

            print(f"Restoration message: {messageID} is deleted")
            print(f"Process for restoration message: {messageID} is completed")





### EOF