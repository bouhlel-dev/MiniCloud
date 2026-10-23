from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models.deletion import ProtectedError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, FormView, ListView, UpdateView

from .forms import ServerProvisionForm, ServerTemplateForm, ServerUpdateForm
from .models import Server, ServerTemplate
from .services import (
    ServerServiceError,
    delete_server,
    postgres_connection_string,
    provision_server,
    restart_server,
    start_server,
    stop_server,
)


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_staff


class OwnedServerQuerysetMixin(LoginRequiredMixin):
    def get_queryset(self):
        return Server.objects.filter(owner=self.request.user).exclude(status=Server.Status.DELETED)


class ServerListView(OwnedServerQuerysetMixin, ListView):
    model = Server
    context_object_name = "servers"
    template_name = "servers/server_list.html"


class ServerDetailView(OwnedServerQuerysetMixin, DetailView):
    model = Server
    context_object_name = "server"
    template_name = "servers/server_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        snapshots = list(self.object.metric_snapshots.order_by("-timestamp")[:60])
        context["metric_data"] = [
            {
                "timestamp": snapshot.timestamp.isoformat(),
                "cpu_percent": float(snapshot.cpu_percent),
                "memory_mb": float(snapshot.memory_mb),
            }
            for snapshot in reversed(snapshots)
        ]
        context["incidents"] = self.object.incidents.all()[:8]
        context["open_incidents"] = self.object.incidents.filter(status="open").count()
        context["postgres_connection_string"] = postgres_connection_string(self.object)
        return context


class ServerMetricsView(OwnedServerQuerysetMixin, View):
    def get(self, request, pk):
        server = get_object_or_404(self.get_queryset(), pk=pk)
        snapshots = server.metric_snapshots.order_by("-timestamp")[:60]
        data = [
            {
                "timestamp": snapshot.timestamp.isoformat(),
                "cpu_percent": float(snapshot.cpu_percent),
                "memory_mb": float(snapshot.memory_mb),
            }
            for snapshot in reversed(list(snapshots))
        ]
        return JsonResponse({"server": server.name, "metrics": data})


class ServerCreateView(LoginRequiredMixin, FormView):
    form_class = ServerProvisionForm
    template_name = "servers/server_form.html"

    def form_valid(self, form):
        try:
            server = provision_server(self.request.user, **form.provisioning_data())
        except ServerServiceError as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        messages.success(self.request, f"Server '{server.name}' is running.")
        return redirect("servers:detail", pk=server.pk)


class ServerUpdateView(OwnedServerQuerysetMixin, UpdateView):
    model = Server
    form_class = ServerUpdateForm
    context_object_name = "server"
    template_name = "servers/server_form.html"

    def form_valid(self, form):
        messages.success(self.request, "Server name updated.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("servers:detail", kwargs={"pk": self.object.pk})


class ServerActionView(OwnedServerQuerysetMixin, View):
    service_method = None
    success_message = ""

    def post(self, request, pk):
        server = get_object_or_404(self.get_queryset(), pk=pk)
        try:
            self.service_method(server)
        except ServerServiceError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, self.success_message.format(name=server.name))
        return redirect("servers:detail", pk=server.pk)


class ServerStartView(ServerActionView):
    service_method = staticmethod(start_server)
    success_message = "Server '{name}' started."


class ServerStopView(ServerActionView):
    service_method = staticmethod(stop_server)
    success_message = "Server '{name}' stopped."


class ServerRestartView(ServerActionView):
    service_method = staticmethod(restart_server)
    success_message = "Server '{name}' restarted."


class ServerDeleteView(OwnedServerQuerysetMixin, DeleteView):
    model = Server
    context_object_name = "server"
    template_name = "servers/server_confirm_delete.html"
    success_url = reverse_lazy("servers:list")

    def form_valid(self, form):
        try:
            delete_server(self.object)
        except ServerServiceError as error:
            messages.error(self.request, str(error))
            return redirect("servers:detail", pk=self.object.pk)
        messages.success(self.request, f"Server '{self.object.name}' was deleted.")
        return redirect(self.get_success_url())


class ServerTemplateListView(StaffRequiredMixin, ListView):
    model = ServerTemplate
    context_object_name = "templates"
    template_name = "servers/server_template_list.html"


class ServerTemplateCreateView(StaffRequiredMixin, CreateView):
    model = ServerTemplate
    form_class = ServerTemplateForm
    template_name = "servers/server_template_form.html"
    success_url = reverse_lazy("servers:template-list")


class ServerTemplateUpdateView(StaffRequiredMixin, UpdateView):
    model = ServerTemplate
    form_class = ServerTemplateForm
    template_name = "servers/server_template_form.html"
    success_url = reverse_lazy("servers:template-list")


class ServerTemplateDeleteView(StaffRequiredMixin, DeleteView):
    model = ServerTemplate
    context_object_name = "template"
    template_name = "servers/server_template_confirm_delete.html"
    success_url = reverse_lazy("servers:template-list")

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ProtectedError:
            messages.error(self.request, "Templates assigned to servers cannot be deleted.")
            return redirect(self.get_success_url())
