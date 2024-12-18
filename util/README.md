# GAS Utilities
/notify
* `notify.py` - Sends notification email on completion of annotation job
* `notify_config.ini` - Configuration options for notification utility
* `run_notify.sh` - Runs the notifications utility script

/archive
* `archive_app.py` - Archives free user result files to Glacier using a Flask app
* `archive_app_config.py` - Configuration options for archive utility Flask app

The archive Flask app must listen on port 5001 (not 5000).

/thaw
* `thaw_app.py` - Thaws an archived Glacier object using a Flask app
* `thaw_app_config.py` - Configuration options for thaw utility Flask app

The archive Flask app must listen on port 5002 (not 5000).

/restore
* `restore.py` - The code for your AWS Lambda function that restores thawed objects to S3
