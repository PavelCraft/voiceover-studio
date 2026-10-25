from django.contrib import admin

from .models import BillingAccount, ProcessingJob, Project, UsageRecord


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "processing_status", "output_mode", "created_at")
    list_filter = ("processing_status", "output_mode", "retention_days")
    search_fields = ("id", "owner__username", "audio_file", "video_file")


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "kind", "status", "attempts", "created_at")
    list_filter = ("kind", "status")


@admin.register(UsageRecord)
class UsageRecordAdmin(admin.ModelAdmin):
    list_display = ("owner", "project", "operation", "media_seconds", "created_at")
    list_filter = ("operation",)


admin.site.register(BillingAccount)
