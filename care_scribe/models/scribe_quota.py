from care.utils.models.base import BaseModel
from care.facility.models.facility import Facility
from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError

User = get_user_model()

start_of_month = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
end_of_month = (start_of_month + timezone.timedelta(days=31)).replace(day=1) - timezone.timedelta(seconds=1)

class ScribeQuota(BaseModel):
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="created_scribe_quota", null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="scribe_quota", null=True, blank=True)
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name="scribe_quota", null=True, blank=True)
    tokens = models.IntegerField(default=0, help_text="Total tokens available for the user/facility monthly")
    allow_ocr = models.BooleanField(default=False, help_text="Whether the user/facility is allowed to use OCR features")

    # either user or facility must be set, not both
    class Meta:
        unique_together = (("user", "facility"),)
        verbose_name = "Scribe Quota"
        verbose_name_plural = "Scribe Quotas"

    def clean(self):
        super().clean()
        if not self.user and not self.facility:
            raise ValidationError("Either 'user' or 'facility' must be set.")
        if self.user and self.facility:
            raise ValidationError("'user' and 'facility' cannot both be set.")
        if self.user and ScribeQuota.objects.filter(user=self.user).exclude(id=self.id).exists():
            raise ValidationError(f"A ScribeQuota already exists for user {self.user}.")
        if self.facility and ScribeQuota.objects.filter(facility=self.facility).exclude(id=self.id).exists():
            raise ValidationError(f"A ScribeQuota already exists for facility {self.facility}.")

    def used(self, from_date=start_of_month, to_date=end_of_month):
        if self.user:
            scribes = self.user.scribe_set.filter(
                created_date__gte=from_date,
                created_date__lte=to_date
            )
        else:
            scribes = self.facility.scribe_set.filter(
                created_date__gte=from_date,
                created_date__lte=to_date
            )

        total_tokens = 0
        for scribe in scribes:
            iterations = scribe.meta.get("iterations", [])
            for iteration in iterations:
                completion_input_tokens = iteration.get("completion_input_tokens", 0) or 0
                completion_output_tokens = iteration.get("completion_output_tokens", 0) or 0
                total_tokens += completion_input_tokens + completion_output_tokens

        return total_tokens

    def __str__(self):
        return f"{self.user.username if self.user else self.facility.name} - {self.tokens} tokens"

    def save(self, *args, **kwargs):
        self.full_clean()  # Ensure clean is called before saving
        super().save(*args, **kwargs)
