## GAS Web Server
This directory contains the Flask-based web app for the GAS.

Endpoints are implemented in `views.py` and rendered through Jinja2 templates in `/templates`. Static variables are declared in `config.py` and accessed via the `app.config` object.

The web server must listen for requests on port 4433.
