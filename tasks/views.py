import re

from django.db.models import Q
from rest_framework import permissions, viewsets
from rest_framework.authentication import BasicAuthentication
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination

from .models import Task
from .serializers import TaskSerializer


class TaskPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


class TaskViewSet(viewsets.ModelViewSet):
    serializer_class = TaskSerializer
    authentication_classes = [BasicAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = TaskPagination

    def get_queryset(self):
        return Task.objects.filter(owner=self.request.user).order_by(
            "-created_at", "-id"
        )

    def filter_queryset(self, queryset):
        if self.action != "list":
            return queryset

        status_filter = self.request.query_params.get("status", "")
        if status_filter:
            valid_statuses = {value for value, _label in Task.STATUS_CHOICES}
            if status_filter not in valid_statuses:
                choices = ", ".join(sorted(valid_statuses))
                raise ValidationError(
                    {"status": f"Choose a valid status: {choices}."}
                )
            queryset = queryset.filter(status=status_filter)

        search_term = self.request.query_params.get("search", "").strip()
        if search_term:
            search_pattern = re.escape(search_term)
            queryset = queryset.filter(
                Q(title__iregex=search_pattern)
                | Q(description__iregex=search_pattern)
            )

        return queryset

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
