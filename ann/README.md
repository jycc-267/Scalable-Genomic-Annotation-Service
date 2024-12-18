This directory must contain the annotator related files:
* `annotator_webhook.py` - Annotator Flask app
* `annotator_webhook_config.py` - Configuration options for annotator_webhook.
* `run.py` - Runs the annotator Flask app
* `annotator_config.ini` - Common configuration options for annotator.py and run.py
* `run_ann.sh` - Runs the annotator script

The annotator Flask app must listen for requests on port 5000, as defined in `run_ann_webhook.sh`.
