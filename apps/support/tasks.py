import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from .models import SupportTicket

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_support_ticket_email(self, ticket_id: str):
    try:
        ticket = SupportTicket.objects.get(id=ticket_id)
    except SupportTicket.DoesNotExist:
        logger.error("Обращение %s не найдено", ticket_id)
        return

    subject = f"Новое обращение VideoLab AI от {ticket.email}"
    context = {
        "email": ticket.email,
        "message": ticket.message,
        "ticket_id": str(ticket.id),
        "created_at": ticket.created_at,
    }

    try:
        html_body = render_to_string("emails/support_ticket_email.html", context)
        text_body = strip_tags(html_body)
    except Exception:
        html_body = f"<p>Обращение от {ticket.email}:</p><p>{ticket.message}</p>"
        text_body = f"Обращение от {ticket.email}:\n{ticket.message}"

    try:
        mail = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "support@videolab.ai"),
            to=getattr(settings, "SUPPORT_EMAIL", ["support@videolab.ai"]),
            reply_to=[ticket.email],
        )
        mail.attach_alternative(html_body, "text/html")
        mail.send(fail_silently=False)

        ticket.status = SupportTicket.Status.SENT
        ticket.error_message = ""
        ticket.save(update_fields=["status", "error_message", "updated_at"])
        logger.info("Письмо по обращению %s отправлено", ticket_id)

    except Exception as exc:
        logger.exception("Не удалось отправить письмо по обращению %s", ticket_id)
        ticket.status = SupportTicket.Status.FAILED
        ticket.error_message = str(exc)
        ticket.save(update_fields=["status", "error_message", "updated_at"])
        raise self.retry(exc=exc)
