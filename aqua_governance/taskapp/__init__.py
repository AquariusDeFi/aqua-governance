import os

from celery import Celery
from celery.schedules import crontab
from django.conf import settings

if not settings.configured:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("aqua_governance")

app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(lambda: settings.INSTALLED_APPS)
app.conf.timezone = "UTC"


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):

    app.conf.beat_schedule.update(
        {
            "aqua_governance.governance.tasks.task_update_active_proposals": {
                "task": "aqua_governance.governance.tasks.task_update_active_proposals",
                "schedule": crontab(minute="*/5"),
                "args": (),
            },
            "aqua_governance.governance.tasks.task_sync_proposal_statuses_by_time": {
                "task": "aqua_governance.governance.tasks.task_sync_proposal_statuses_by_time",
                "schedule": crontab(minute="*/1"),
                "args": (),
            },
            "aqua_governance.governance.tasks.task_check_expired_proposals": {
                "task": "aqua_governance.governance.tasks.task_check_expired_proposals",
                "schedule": crontab(minute="0", hour="*/24"),
                "args": (),
            },
            "aqua_governance.governance.tasks.task_check_pending_proposal_payments": {
                "task": "aqua_governance.governance.tasks.task_check_pending_proposal_payments",
                "schedule": crontab(minute="*/1"),
                "args": (),
            },
            "aqua_governance.governance.tasks.task_update_votes": {
                "task": "aqua_governance.governance.tasks.task_update_votes",
                "schedule": crontab(minute="*/10"),
                "args": (),
            },
            "aqua_governance.governance.tasks.task_poll_submitted_onchain_executions": {
                "task": "aqua_governance.governance.tasks.task_poll_submitted_onchain_executions",
                "schedule": crontab(minute="*/1"),
                "args": (),
            },
            "aqua_governance.governance.tasks.task_retry_failed_onchain_executions": {
                "task": "aqua_governance.governance.tasks.task_retry_failed_onchain_executions",
                "schedule": crontab(minute="*/10"),
                "args": (),
            },
        }
    )
