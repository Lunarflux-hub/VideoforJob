from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import SupportTicket
from .serializers import SupportTicketCreateSerializer, SupportTicketSerializer
from .tasks import send_support_ticket_email


def _get_client_ip(request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class SupportTicketCreateView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support"

    def post(self, request):
        serializer = SupportTicketCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"success": False, "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ticket = serializer.save(ip_address=_get_client_ip(request))
        send_support_ticket_email.delay(str(ticket.id))

        return Response(
            {
                "success": True,
                "message": "Спасибо, мы ответим вам в ближайшее время",
                "ticket": SupportTicketSerializer(ticket).data,
            },
            status=status.HTTP_201_CREATED,
        )
