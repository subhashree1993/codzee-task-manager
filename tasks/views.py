from rest_framework import permissions, viewsets
from rest_framework.authentication import BasicAuthentication

from .models import Task
from .serializers import TaskSerializer


class TaskViewSet(viewsets.ModelViewSet):
    serializer_class = TaskSerializer
    authentication_classes = [BasicAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Task.objects.filter(owner=self.request.user).order_by(
            "-created_at", "-id"
        )

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
