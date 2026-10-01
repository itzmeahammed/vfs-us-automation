"""The process-wide JobManager.

One instance for the API process. Its own module so that every handler, the
app's lifespan and the tests share it without importing the app.
"""

from src.api.modules.jobs.manager import JobManager

job_manager = JobManager()
