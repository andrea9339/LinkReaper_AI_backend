# One active client's search, with capacity for downloads and health checks.
# Gunicorn 23's gthread loop reports worker liveness independently of requests.
workers = 1
worker_class = "gthread"
threads = 4
timeout = 60
