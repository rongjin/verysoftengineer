# Loaded automatically when you run `gunicorn wsgi:app` from this folder.
import os

# Short links are kept in memory, so all requests must reach the same process.
# More workers would each have their own links, and short links would randomly 404.
workers = 1
threads = 4

# Set PORT to change the port, e.g. `PORT=9000 gunicorn wsgi:app`.
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
accesslog = "-"
