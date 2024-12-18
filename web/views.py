# views.py
#
# Application logic for the GAS
##

import os
import uuid
import time
import json
import pytz
from datetime import datetime

import boto3
from botocore.client import Config
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from flask import abort, flash, redirect, render_template, request, session, url_for

import stripe
from auth import update_profile

# Defined in the app.py file:
    # app usually refers to the Flask application instance created with Flask(__name__)
    # db typically refers to the SQLAlchemy database instance created with SQLAlchemy(app)
# This import statement is often used in other parts of a Flask application (like in models, views...) to access the main Flask application instance and the database connection
from app import app, db

#Defined in the decorators.py file
from decorators import authenticated, is_premium

"""Start annotation request
Create the required AWS S3 policy document and render a form for
uploading an annotation input file using the policy document

Note: You are welcome to use this code instead of your own
but you can replace the code below with your own if you prefer.
"""


@app.route("/annotate", methods=["GET"])
@authenticated
def annotate():

    # Create a low-level s3 client
    # For boto S3 services, refer to: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html
    # Specify the use of Version 4 signatures: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/config.html
    s3 = boto3.client(
        "s3",
        region_name = app.config["AWS_REGION_NAME"],
        config = Config(signature_version="s3v4")
    )


    # Define S3 policy fields and conditions
    # For definition of S3 jargons: https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html
    # For the meaning of each fields/conditions, refer to "POST Object - Form Fields": https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectPOST.html
    # For upload mechanism: https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-UsingHTTPPOST.html


    bucket_name = app.config["AWS_S3_INPUTS_BUCKET"]

    # The authenticated user’s ID parsed from the flask session object
    user_id = session["primary_identity"]


    # Generate unique ID to be used as S3 key so that each object (uploaded file) has a unique identifier (or "file name").
    # Using ${filename} at the end of the key s.t. the uploaded file's name is automatically appended
    # Allow multiple files to be uploaded with a given prefix
    key_name = (
        app.config["AWS_S3_KEY_PREFIX"]
        + user_id
        + "/"
        + str(uuid.uuid4())
        + "~${filename}"
    )


    # Create the redirect URL
    # flask.Request.url: https://flask.palletsprojects.com/en/stable/api/#flask.Request.url
    redirect_url = str(request.url) + "/job"

    # Define presigned policy conditions
    encryption = app.config["AWS_S3_ENCRYPTION"]

    acl = app.config["AWS_S3_ACL"]

    fields = {

        # Where the user should be directed upon successful upload
        "success_action_redirect": redirect_url,

        "x-amz-server-side-encryption": encryption,

        # The specified S3 Access Control List
        "acl": acl,

        "csrf_token": app.config["SECRET_KEY"],
    }

    conditions = [
        ["starts-with", "$success_action_redirect", redirect_url],
        {"x-amz-server-side-encryption": encryption},
        {"acl": acl},
        ["starts-with", "$csrf_token", ""],
    ]


    # Generate the presigned POST request
    try:
        # S3.Client.generate_presigned_post: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/generate_presigned_post.html
        # For the merit of using generate_presigned_post(): https://stackoverflow.com/questions/65198959/aws-s3-generate-presigned-url-vs-generate-presigned-post-for-uploading-files
        # Coding example: https://docs.aws.amazon.com/code-library/latest/ug/python_3_s3_code_examples.html
        presigned_post = s3.generate_presigned_post(
            Bucket = bucket_name,
            Key = key_name,
            Fields = fields,
            Conditions = conditions,
            ExpiresIn = app.config["AWS_SIGNED_REQUEST_EXPIRATION"],
        )
    except ClientError as e:
        app.logger.error(f"Unable to generate presigned URL for upload: {e}")
        return abort(500)

    # Render the upload form which will parse/submit the presigned POST
    # flask.render_template(): https://flask.palletsprojects.com/en/2.3.x/api/#flask.render_template
    return render_template(
        "annotate.html", s3_post = presigned_post, role = session["role"]
    )


"""Fires off an annotation job
Accepts the S3 redirect GET request, parses it to extract 
required info, saves a job item to the database, and then
publishes a notification for the annotator service.

Note: Update/replace the code below with your own from previous
homework assignments
"""

@app.route("/annotate/job", methods=["GET"])
@authenticated
def create_annotation_job_request():

    region = app.config["AWS_REGION_NAME"]
    user_id = session["primary_identity"] # authenticated user id

    dynamo = boto3.client("dynamodb", region_name = region)
    sns = boto3.client("sns", region_name = region)

    # Parse the S3 redirect URL query parameters for S3 object info
    # request.get_json() is typically used for POST requests with JSON data
    # For GET requests with query parameters, we use request.args instead
    # flask.Request object encapsulates attributes of a http request
    # flask.Request.args: https://flask.palletsprojects.com/en/stable/api/#flask.Request.args
    bucket_name = request.args.get("bucket")
    s3_key = request.args.get("key")
    file_name = os.path.basename(s3_key) # uuid~test.vcf
    job_id, input_file = file_name.split("~", 1) # uuid, test.vcf

# Should we move move dynamodb table attributes into config.py
    # Create a job item 
    # When using the client approach,
    # must pass type information for each item attribute when putting data into the annotation DynamoDB table
    # https://binaryguy.tech/aws/dynamodb/put-items-into-dynamodb-table-using-python/
    data = {
        "job_id": {"S": job_id}, # primary key: prevent duplicates
        "user_id": {"S": user_id}, # the authenticated user’s ID
        "input_file_name": {"S": input_file},
        "s3_inputs_bucket": {"S": bucket_name},
        "s3_key_input_file": {"S": s3_key},
        # DynamoDB stores epoch time as Number
        # Numbers are sent across the network to DynamoDB as strings
        # See DynamoDB API Reference AttributeValue: https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_AttributeValue.html
        # time.time(): https://docs.python.org/3/library/time.html#time.time
        "submit_time": {"N": str(int(time.time()))},
        "job_status": {"S": "PENDING"}
    }


    # Persist job to database
    # NOTE: It's better to have a PENDING job documented in dynamoDB, then a successful SNS publish but the message data is not placed in dynamoDB (the job would never updated as "COMPLETED")
    # So we are inserting data into dynamo table first, and then public a message to the SNS topic
    try:
        # Insert item into the annotations table in DynamoDB
        # DynamoDB.Client.put_item: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb/client/put_item.html
        dynamo.put_item(
            TableName = app.config["AWS_DYNAMODB_ANNOTATIONS_TABLE"],
            Item = data
        )

    # Catch exceptions when adding items to DynamoDB
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        # Error handling with DynamoDB: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Programming.Errors.html
        # DynamoDB Developer Guide: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Programming.Errors.html
        # Rate of requests exceeds the allowed throughput
        if err_code in ("ProvisionedThroughputExceededException", "ThrottlingException"):
            app.logger.error(f"{err_code}: Failed to insert item into DynamoDB Table- {msg}")
            return abort(400)

        # DynamoDB could not process the request
        elif err_code == "InternalServerError":
            app.logger.error(f"{err_code}: Failed to insert item into DynamoDB Table- {msg}")
            return abort(500)

        # Unexpected Error
        else:
            raise e


    # Send message to request queue
    try:
        # SNS.Client.publish(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sns/client/publish.html
        sns_response = sns.publish(

# VARY BY ASSIGNMENT
            # Specify the Amazon Resource Name (ARN) of the SNS topic to which our message is being published
            TopicArn = app.config["AWS_SNS_JOB_REQUEST_TOPIC"],

            # The actual content to be send
            # Convert the Message to a string, this is required!
            # If MessageStructure = "json", the value of the Message parameter must:
                # contain at least a top-level JSON key of “default” with a value that is a string
            # Non-string values within the JSON message will cause the key to be ignored.
            # json.dumps(): https://docs.python.org/3/library/json.html#json.dumps
            # NOTE: json.loads(json.dumps(x)) != x if x has non-string keys
            Message = json.dumps({"default": json.dumps(data)}),

            # More flexible
            MessageStructure = "json"

            # what does the transport protocol represent in SQS?
        )


        # When a messageId is returned, the message is saved
        # And Amazon SNS immediately delivers it to subscribers
        # See https://docs.aws.amazon.com/sns/latest/api/API_Publish.html
        if not sns_response.get("MessageId"):
            app.logger.error(f"Failed to publish message to SNS - No MessageId returned")
            return abort(500)


    # Catch exceptions ocurring while sending a notification message to the topic "jycchien_job_requests"
    # Botocore SNS Client Exceptions: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/services/sns.html#client-exceptions
    # SNS.Client.publish(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/sns/client/publish.html
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code in ("InvalidParameter", "InvalidParameterValue"):
            app.logger.error(f"{err_code}: Invalid parameter for SNS publish - {msg}")
            return abort(400)

        elif err_code == "InternalError":
            app.logger.error(f"{err_code}: Internal SNS error - {msg}")
            return abort(500)

        elif err_code == "NotFound":
            app.logger.error(f"{err_code}: SNS topic not found - {msg}")
            return abort(404)

        else:
            raise e

# This is the end of the HTTP request-response cycle; the rest of the workflow happens asynchronously in the background
    return render_template("annotate_confirm.html", job_id = job_id)



"""List all annotations for the cuurent user
"""


@app.route("/annotations", methods=["GET"])
@authenticated
def annotations_list():

    region = app.config["AWS_REGION_NAME"]
    dynamo = boto3.client("dynamodb", region_name = region)


    # # Check if user is authenticated
    # if not session.get("primary_identity"):
    #     app.logger.error("User not authenticated")
    #     abort(403)  # Unauthorized

    user_id = session["primary_identity"] # authenticated user id

    try:
        # Query the annotation table using secondary index user_id_index to retrieve only the jobs for the current user
        # user_id_index: user_id as partition key and submit_time as sort_key
        # This secondary index will speed up access when retrieving annotations for a specific user
        # The query results will be sorted in the order jobs were submitted
        # DynamoDB.Client.query(): https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb/client/query.html
        response = dynamo.query(

            TableName = app.config["AWS_DYNAMODB_ANNOTATIONS_TABLE"],

            # The name of an secondary index to query, can be either local or global on the main table
            IndexName = app.config["AWS_DYNAMODB_ANNOTATIONS_USER_ID_INDEX"],

            # Returns only the attributes listed in ProjectionExpression
            # This parameter value is equivalent to specifying ProjectionExpression without specifying any value for Select
            # If using the ProjectionExpression parameter, then the value for Select can only be SPECIFIC_ATTRIBUTES
            Select = "SPECIFIC_ATTRIBUTES",

            # Use the KeyConditionExpression parameter to provide a specific value for the partition key
            # The condition must perform an equality test on a single partition key value, this is the diff compared to scan()
            # Return all of the items from the table or index with that partition key value
            # Can optionally narrow the scope of the Query operation by specifying a sort key value and a comparison operator 
            KeyConditionExpression = "user_id = :uid",

            ExpressionAttributeValues = {
                ":uid": {"S": user_id}
            },

            # A string that identifies one or more attributes to retrieve from the table
            ProjectionExpression = "job_id, submit_time, input_file_name, job_status"

            # ExclusiveStartKey?
        )

    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code == "ResourceNotFoundException":
            app.logger.error(f"{err_code}: Table or index not found - {msg}")
            abort(404)
        elif err_code == "ProvisionedThroughputExceededException":
            app.logger.error(f"{err_code}: Exceeded provisioned throughput - {msg}")
            abort(503)
        elif err_code == "RequestLimitExceeded":
            app.logger.error(f"{err_code}: Query exceeds request limit - {msg}")
            abort(500)
        elif err_code == "InternalServerError":
            app.logger.error(f"{err_code}: Failed to query item in annotation table - {msg}")
            abort(500)
        else:
            app.logger.error(f"Unexpected ClientError: DynamoDB query failed - {str(e)}")
            abort(500)

    except Exception as e:
        app.logger.error(f"Unexpected error: DynamoDB query failed - {str(e)}")
        abort(500)


    # Process the query results
    ann_jobs = []
    cst = pytz.timezone("America/Chicago")

    # Get an array of items and their attributes that match the query criteria.
    for item in response.get("Items", []):
        try:
            # Convert epoch timestamp to datetime in CST
            # Converting between timezones from UTC to CST using the standard astimezone method
            # datetime.fromtimestamp: https://docs.python.org/3/library/datetime.html#datetime.datetime.fromtimestamp
            # Working with datetime using pytz examples: https://github.com/stub42/pytz/blob/master/src/README.rst#example--usage
            submit_time = datetime.fromtimestamp(
                int(item["submit_time"]["N"]), 
                tz = pytz.UTC
            ).astimezone(cst)

            # Prepare the attributes to present for the web page
            ann_jobs.append({
                "job_id": item["job_id"]["S"],
                "submit_time": submit_time.strftime("%Y-%m-%d @ %H:%M:%S"),
                "input_file_name": item["input_file_name"]["S"],
                "job_status": item["job_status"]["S"]
            })

        except (KeyError, ValueError) as e:
            app.logger.error(f"Error processing job item, missing targeted attributes: {str(e)}")
            continue

    # Sort annotation jobs by submit time (descending order)
    ann_jobs.sort(key = lambda x: x["submit_time"], reverse = True)


    return render_template("annotations.html", ann_jobs = ann_jobs, has_jobs = len(ann_jobs) > 0)


"""Display details of a specific annotation job
"""


@app.route("/annotations/<job_id>", methods=["GET"])
@authenticated
def annotation_details(job_id):

    region = app.config["AWS_REGION_NAME"]
    dynamo = boto3.client("dynamodb", region_name = region)
    s3 = boto3.client(
        "s3",
        region_name = app.config["AWS_REGION_NAME"],
        config = Config(signature_version="s3v4")
    )

    user_id = session["primary_identity"] # authenticated user id
    user_role = session["role"] # user role

    try:
        # DynamoDB.Client.get_item: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb/client/get_item.html
        response = dynamo.get_item(

            TableName = app.config["AWS_DYNAMODB_ANNOTATIONS_TABLE"],

            # Use the KeyConditionExpression parameter to provide a specific value for the partition key
            Key = {
                "job_id": {"S": job_id}
            }
        )

    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code == "ResourceNotFoundException":
            app.logger.error(f"{err_code}: Table or Job item not found - {msg}")
            abort(404)
        elif err_code == "ProvisionedThroughputExceededException":
            app.logger.error(f"{err_code}: Exceeded provisioned throughput - {msg}")
            abort(503)
        elif err_code == "RequestLimitExceeded":
            app.logger.error(f"{err_code}: Query exceeds request limit - {msg}")
            abort(500)
        elif err_code == "InternalServerError":
            app.logger.error(f"{err_code}: Failed to query item in annotation table - {msg}")
            abort(500)
        else:
            app.logger.error(f"Unexpected ClientError: DynamoDB query failed - {str(e)}")
            abort(500)

    except Exception as e:
        app.logger.error(f"Unexpected error: DynamoDB query failed - {str(e)}")
        abort(500)

    if not response.get("Item"):
        app.logger.error(f"Job {job_id} not found")
        abort(404)

    # Should be of length 1
    ann_job = response.get("Item")

    # Check if job belongs to current user
    if ann_job.get("user_id", {}).get("S") != user_id:
        app.logger.error("User unauthorized to view this annotation")
        abort(403)



    # Get displaying attribute values from the annotation table
    try:
        cst = pytz.timezone("America/Chicago")

        submit_time = datetime.fromtimestamp(
            int(ann_job["submit_time"]["N"]), 
            tz = pytz.UTC
        ).astimezone(cst)

        # Initialize a variable to 0 (in sec) before job is completed
        elapse = 0

        # Create pre-signed URL for input file, the user can download the S3 object by entering the presigned URL in a browser
        # S3.Client.generate_presigned_url: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/generate_presigned_url.html
        # Presigned URLs examples: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3-presigned-urls.html
        input_file_url = s3.generate_presigned_url(

            "get_object",

            Params = {
                "Bucket": ann_job["s3_inputs_bucket"]["S"],
                "Key": ann_job["s3_key_input_file"]["S"]
            },

            ExpiresIn = app.config["AWS_SIGNED_REQUEST_EXPIRATION"]

        )

        # Prepare job details
        ann_job_details = {
            "job_id": ann_job["job_id"]["S"],
            "submit_time": submit_time.strftime("%Y-%m-%d @ %H:%M:%S"),
            "input_file_name": ann_job["input_file_name"]["S"],
            "input_file_url": input_file_url,
            "status": ann_job["job_status"]["S"]
        }


        # Add completed job specific details
        if ann_job["job_status"]["S"] == "COMPLETED":

            complete_time = datetime.fromtimestamp(
                int(ann_job["complete_time"]["N"]), 
                tz = pytz.UTC
            ).astimezone(cst)

            # datetime.now(): https://docs.python.org/3/library/datetime.html#datetime.datetime.now
            current_time = datetime.now(cst)

            # total_seconds(): https://docs.python.org/3/library/datetime.html#datetime.timedelta.total_seconds
            elapse = (current_time - complete_time).total_seconds()

            # Check the session user is free user or premium user
            # If it's free user, provide download link of result file for only 3 min after job completion
            if (user_role == "free_user" and elapse <= app.config["FREE_USER_DATA_RETENTION"]) or (user_role == "premium_user" and "results_file_archive_id" not in ann_job):

                # Generate pre-signed URLs for result and log files
                result_file_url = s3.generate_presigned_url(
                    "get_object",
                    Params = {
                        "Bucket": ann_job["s3_results_bucket"]["S"],
                        "Key": ann_job["s3_key_result_file"]["S"]
                    },
                    ExpiresIn = app.config["AWS_SIGNED_REQUEST_EXPIRATION"]
                )

                ann_job_details.update({
                    "complete_time": complete_time.strftime("%Y-%m-%d @ %H:%M:%S"),
                    "result_file_url": result_file_url
                })
            elif (user_role == "premium_user") and ("results_file_archive_id" in ann_job):
                ann_job_details.update({
                    "complete_time": complete_time.strftime("%Y-%m-%d @ %H:%M:%S"),
                    "restore_message": "File is being thawed for restoration; please check back later"
                })
            else:
                ann_job_details.update({
                    "complete_time": complete_time.strftime("%Y-%m-%d @ %H:%M:%S")
                })

    # S3 Client Exceptions: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#client-exceptions
    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code in ("NoSuchKey", "NoSuchBucket"):
            app.logger.error(f"{err_code}: Bucket or File not found in S3 - {msg}")
            abort(404)
        else:
            app.logger.error(f"Unexpected ClientError: S3 error generating presigned URL - {msg}")
            abort(500)

    except Exception as e:
        app.logger.error(f"Unexpected error: S3 error generating presigned URL - {str(e)}")
        abort(500)



    return render_template("annotation.html", ann_job = ann_job_details, elapse = elapse, user_role = user_role)


"""Display the log file contents for an annotation job
"""


@app.route("/annotations/<job_id>/log", methods=["GET"])
@authenticated
def annotation_log(job_id):

    region = app.config["AWS_REGION_NAME"]
    dynamo = boto3.client("dynamodb", region_name = region)
    s3 = boto3.client("s3", region_name = region)
    user_id = session["primary_identity"]

    try:
        # DynamoDB.Client.get_item: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb/client/get_item.html
        response = dynamo.get_item(

            TableName = app.config["AWS_DYNAMODB_ANNOTATIONS_TABLE"],

            # Use the KeyConditionExpression parameter to provide a specific value for the partition key
            Key = {
                "job_id": {"S": job_id}
            }

        )

    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code == "ResourceNotFoundException":
            app.logger.error(f"{err_code}: Table or Job item not found - {msg}")
            abort(404)
        elif err_code == "ProvisionedThroughputExceededException":
            app.logger.error(f"{err_code}: Exceeded provisioned throughput - {msg}")
            abort(500)
        elif err_code == "RequestLimitExceeded":
            app.logger.error(f"{err_code}: Query exceeds request limit - {msg}")
            abort(500)
        elif err_code == "InternalServerError":
            app.logger.error(f"{err_code}: Failed to query item in annotation table - {msg}")
            abort(500)
        else:
            app.logger.error(f"Unexpected ClientError: DynamoDB query failed - {str(e)}")
            abort(500)

    except Exception as e:
        app.logger.error(f"Unexpected error: DynamoDB query failed - {str(e)}")
        abort(500)

    if not response.get("Item"):
        app.logger.error(f"Job {job_id} not found")
        abort(404)

    # Should be of length 1
    ann_job = response.get("Item")

    # Check if job belongs to current user
    if ann_job.get("user_id", {}).get("S") != user_id:
        app.logger.error("User unauthorized to view this annotation")
        abort(403)

    # Retrieve the log file content from S3
    try:
        log_key = ann_job["s3_key_log_file"]["S"]

        # Different from the generate_presigned_url() above, we call the client method get_object() directly
        # S3.Client.get_object: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/get_object.html
        log_file = s3.get_object(Bucket = app.config["AWS_S3_RESULTS_BUCKET"], Key = log_key)

        # Read all data into bytes from Object data 'log_file["Body"]', and decode the byte data 
        # log_file["Body"] is of StreamingBody class
        # botocore.response.StreamingBody: https://botocore.amazonaws.com/v1/documentation/api/latest/reference/response.html#botocore.response.StreamingBody
        log_content = log_file["Body"].read().decode("utf-8")

    except ClientError as e:
        msg = e.response["Error"]["Message"]
        err_code = e.response["Error"]["Code"]

        if err_code in ("NoSuchKey", "NoSuchBucket"):
            app.logger.error(f"{err_code}: Bucket or File not found in S3 - {msg}")
            abort(404)
        else:
            app.logger.error(f"Unexpected ClientError: S3 error getting log file content - {msg}")
            abort(500)

    except Exception as e:
        app.logger.error(f"Unexpected error: S3 error getting log file content - {str(e)}")
        abort(500)

    return render_template("view_log.html", log_content = log_content, job_id = job_id)


@app.route("/subscribe", methods=["GET", "POST"])
def subscribe():

    # Resuests from /profile or annotation.html
    if request.method == "GET":

        # Display the subscription form
        if session.get("role") == "free_user":
            return render_template("subscribe.html")
        else:
            return redirect(url_for("profile"))

    # Requests from subscribe.html
    elif request.method == "POST":

        region = app.config["AWS_REGION_NAME"]
        sns = boto3.client("sns", region_name = region)

        try:
            # Extract the Stripe token from the submitted form (containing a user’s card details)
            # See scripts.html for enabling publishable keys for card tokenization
            # Request.form: https://flask.palletsprojects.com/en/stable/api/#flask.Request.form
            # Example: https://www.digitalocean.com/community/tutorials/processing-incoming-request-data-in-flask#using-form-data
            stripe_token = request.form.get("stripe_token")
        except Exception as e:
            app.logger.error(f"Error retrieving Stripe token: {str(e)}")
            abort(500)

        try:
            # Get user information from the session
            user_id = session["primary_identity"]
            user_email = session["email"]
            user_name = session["name"]
        except Exception as e:
            app.logger.error(f"Error retrieving user information: {str(e)}")
            abort(500)

        try:
            # Create a customer on Stripe, will return a customer object after successful creation
            # Stripe api example: https://docs.stripe.com/api/customers/create?lang=python
            stripe.api_key = app.config["STRIPE_SECRET_KEY"]
            customer = stripe.Customer.create(

                email = user_email,

                name = user_name,

                # When using payment sources created via the Token or Sources APIs, passing source will create a new source object that holds a user’s card details 
                # See also Card Creation API: https://docs.stripe.com/api/cards/object
                source = stripe_token

            )
        # Stripe Error Handling: https://docs.stripe.com/error-handling#catch-exceptions
        except stripe.error.StripeError as e:
            app.logger.error(f"Stripe error: status code - {e.http_status}, msg - {e.user_message}")
            abort(500)

        try:
            # Subscribe customer to pricing plan
            # Stripe api example: https://docs.stripe.com/api/subscriptions/create?lang=python
            subscription = stripe.Subscription.create(

                # The customer object: https://docs.stripe.com/api/customers/object?lang=python
                customer = customer.id,

                items = [{"price": app.config["STRIPE_PRICE_ID"]}]

            )
        except stripe.error.StripeError as e:
            app.logger.error(f"Stripe error: status code - {e.http_status}, msg - {e.user_message}")
            abort(500)

        # Update user role in accounts database
        # This function already handle the potential errors
        update_profile(identity_id = user_id, role = "premium_user")

        # Update the user's role in the session
        session["role"] = "premium_user"

        # Request restoration of the user's data from Glacier
        # ...add code here to initiate restoration of archived user data
        # ...and make sure you handle files pending archive!

        # If the session's user upgrades to preimum, publish the user id to SNS topic for "thawing" result archives belonging to this user
        # To search result archives, retrieving results_file_archive_id from dynamoDB is required
        thawing_data = { "user_id": session["primary_identity"]}

        try:
            sns_response = sns.publish(
                TopicArn = app.config["AWS_SNS_THAW_TOPIC"],
                Message = json.dumps({"default": json.dumps(thawing_data)}),
                MessageStructure = "json"
            )

            # When a messageId is returned, the message is saved
            # And Amazon SNS immediately delivers it to subscribers
            if not sns_response.get("MessageId"):
                app.logger.error(f"Failed to publish message to Thaw/Restore SNS - No MessageId returned")
                return abort(500)

        except ClientError as e:
            msg = e.response["Error"]["Message"]
            err_code = e.response["Error"]["Code"]

            if err_code in ("InvalidParameter", "InvalidParameterValue"):
                app.logger.error(f"{err_code}: Invalid parameter for SNS publish - {msg}")
                return abort(400)

            elif err_code == "InternalError":
                app.logger.error(f"{err_code}: Internal SNS error - {msg}")
                return abort(500)

            elif err_code == "NotFound":
                app.logger.error(f"{err_code}: SNS topic not found - {msg}")
                return abort(404)

            else:
                app.logger.error(f"{err_code}: Unexpected SNS error - {msg}")
                return abort(500)

        # Render the confirmation page
        return render_template("subscribe_confirm.html", stripe_id = customer.id)

"""DO NOT CHANGE CODE BELOW THIS LINE
*******************************************************************************
"""

"""Set premium_user role
"""


@app.route("/make-me-premium", methods=["GET"])
@authenticated
def make_me_premium():
    # Hacky way to set the user's role to a premium user; simplifies testing
    update_profile(identity_id=session["primary_identity"], role="premium_user")
    return redirect(url_for("profile"))


"""Reset subscription
"""


@app.route("/unsubscribe", methods=["GET"])
@authenticated
def unsubscribe():
    # Hacky way to reset the user's role to a free user; simplifies testing
    update_profile(identity_id=session["primary_identity"], role="free_user")
    return redirect(url_for("profile"))


"""Home page
"""


@app.route("/", methods=["GET"])
def home():
    return render_template("home.html"), 200


"""Login page; send user to Globus Auth
"""


@app.route("/login", methods=["GET"])
def login():
    app.logger.info(f"Login attempted from IP {request.remote_addr}")
    # If user requested a specific page, save it session for redirect after auth
    if request.args.get("next"):
        session["next"] = request.args.get("next")
    return redirect(url_for("authcallback"))


"""404 error handler
"""


@app.errorhandler(404)
def page_not_found(e):
    return (
        render_template(
            "error.html",
            title="Page not found",
            alert_level="warning",
            message="The page you tried to reach does not exist. \
      Please check the URL and try again.",
        ),
        404,
    )


"""403 error handler
"""


@app.errorhandler(403)
def forbidden(e):
    return (
        render_template(
            "error.html",
            title="Not authorized",
            alert_level="danger",
            message="You are not authorized to access this page. \
      If you think you deserve to be granted access, please contact the \
      supreme leader of the mutating genome revolutionary party.",
        ),
        403,
    )


"""405 error handler
"""


@app.errorhandler(405)
def not_allowed(e):
    return (
        render_template(
            "error.html",
            title="Not allowed",
            alert_level="warning",
            message="You attempted an operation that's not allowed; \
      get your act together, hacker!",
        ),
        405,
    )


"""500 error handler
"""


@app.errorhandler(500)
def internal_error(error):
    return (
        render_template(
            "error.html",
            title="Server error",
            alert_level="danger",
            message="The server encountered an error and could \
      not process your request.",
        ),
        500,
    )


"""CSRF error handler
"""


from flask_wtf.csrf import CSRFError


@app.errorhandler(CSRFError)
def csrf_error(error):
    return (
        render_template(
            "error.html",
            title="CSRF error",
            alert_level="danger",
            message=f"Cross-Site Request Forgery error detected: {error.description}",
        ),
        400,
    )


### EOF
