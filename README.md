# GAS Framework
An enhanced web framework (based on [Flask](https://flask.palletsprojects.com/)) for use in the capstone project. Adds robust user authentication (via [Globus Auth](https://docs.globus.org/api/auth)), modular templates, and some simple styling based on [Bootstrap](https://getbootstrap.com/docs/3.3/).

Directory contents are as follows:
* `/web` - The GAS web app files
* `/ann` - Annotator files
* `/util` - Utility scripts/apps for notifications, archival, and restoration
* `/aws` - AWS user data files

## Archive Process
The design of this periodic background archival task is based on a Flask app that presents an endpoint named `/archive`. This webhook endpoint accept periodic `POST` requests from the `SNS results_archive` topic. The delivery of such POST requests is handled by a AWS Step Functions state machine implemented in `run.py`. After an annotation job is completed, `run.py` starts state machine execution to wait through the time period in which free users are allowed to download the `.annot.vcf` file and then publish the payload to `SNS results_archive`. Finally, inside `archive_app.py` the endpoint `/archive` long-polls the archive message from `SQS results_archive` and does the archival tasks based on the user's role.

### Rationale
At the very beginning I thought I could let the webhook `archive_app.py` listen to the `SNS job_results topic` and parse the data (`request.data`) required by the webhook. But I decided not to do so due to scalability and fault-tolerance concern. Since SNS notifications are sent once only, it is better to retrieve the persisted payload from SQS than parse the it from SNS directly. Having new `SNS/SQS results_archive` topic/queue handle sudden spikes/high volume in archive demand(workload) more properly since this mechanism can distribute tasks across multiple consumers (and we may not want to have a SNS serving different purposes).

Additionally, adding new `results_archive SNS/SQS` is more horizontally scalable and cost-effective than continuous polling since the later approach consumes CPU and network resources, even when there are no tasks. A Step Functions state machine can be triggered multiple times concurrently (and scale individual state as needed); each execution of a state machine is independent. That said, the whenever the web hook `archive_app.py` receives `HTTP POST` multiple requests from `SNS results_archive` (after each execution finishes its waiting task), the endpoint can start the archival task based on the user's role.

## Restoration Process
1. The restoration process is as follows: 
    1. In `web/views.py`, the endpoint `/subscribe` will send a `POST` request to update the user's profile to premium after that free_user has filled up the subscription form run by Stripe (`A15`). Then, this endpoint will triggers a SNS thawing topic `jycchien_results_thaw` to send a message to the thawing queue `jycchien_results_thaw`. 
    2. `util/thaw/thaw_app.py` is a webhook triggered by the SNS thaw topic to poll messages from the corresponding thaw queue. When the webhook receives a `POST` request from the SNS thawing topic, it poll messages from the thawing queue and do the followings:
        1. Extract `user_id` from a received message in order to get all archives belonging to this current premium_user: `dynamodb.query()` the `job_id` (named it as `annotation_job_id`) and `results_file_archive_id` of the archived annotation job items the user has submitted as a free_user.
        2. For each of these `results_file_archive_ids`, the webhook then initiate a job for _archive_retrieval_ on `glacier`, with the SNS restoration topic `jycchien_results_restore` attached. This mechanism allows Glacier to publish a restoration message containing `annotation_job_id` as `JobDescription` when the thawing job is completed and the output of the thawed annotation result file is ready to be downloaded. 
            * In this script, `glacier.initiate_job()` will default to expedited retrieval, but if this leads to `InsufficientCapacityException`, the function will re-run with standard retrieval. 
        3. Delete the thawed message from the thaw queue. 
    3. `util/restore/restore.py` is a Lambda Function triggered by the SNS restoration topic `jycchien_results_restore` to poll messages from the corresponding restoration queue listening to that topic, and when the function is triggered: 
        1. Extract `annotation_job_id`, `glacier JobId`, `ArchiveId` from a received message.
        2. Use the `glacier JobId` included in the restoration message to receive the bytes-file that was un-archived. 
        3. Use `dynamodb.get_item()` to get `s3_key_result_file` for the corresponding `annotation_job_id`.
        4. Put the bytes-file back up to `S3` located at `s3_key_result_file` using `s3.put_object()`.
        5. Delete the archive in Glacier for each processed restoration message with `glacier.delete_archive()`.
        6. Update the corresponding `dynamodb` status to delete the `results_file_archive_id` field so that the front-end webpages are correctly displayed. 
        7. Delete the message from the restoration queue. 

### Rationale
The restoration process is designed with the purpose to enhance scalability as the Lambda function scales up the number of execution environment instances in response if it receives more concurrent triggers. Additionally, implementing a Lambda function is cost-effective and "efficient in terms of input/output operations" due to the fact that serverless architectures have low operational overhead, with Lambda functions being spun up and down transiently.

In terms of preserving reliability, which is probably the most interesting part, I was hesitated between `SNS to Lambda` versus `SNS to SQS to Lambda`. The primary advantage of having a SQS in between SNS and Lambda is _reprocessing_. Assume that the Lambda fails to process certain event for some reason (e.g. timeout or lack of memory footprint), we can increase the timeout (to max 15 minutes) or memory (to max of 1.5GB) and restart the polling to reprocess the older events. This would not be possible in case of `SNS to Lambda`. However, I decided to do `SNS to Lambda` but have a SQS subscribe to that SNS (`jycchien_results_restore`). In this way, I am able to preserve the persistence provided by SQS and maintain the simplicity of implementing `SNS to Lambda` by serving the restoration topic as a pure trigger (i.e. I don't care about `event` sending to the Lambda by SNS) and let the Lambda do the long polling.

Reference: https://stackoverflow.com/questions/42656485/sns-to-lambda-vs-sns-to-sqs-to-lambda