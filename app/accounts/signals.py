"""Signals that keep the account profile lifecycle idempotent."""

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserProfile


@receiver(post_save, sender=get_user_model())
def ensure_user_profile(sender, instance, **kwargs):
    """Ensure every saved Django user has exactly one profile."""

    UserProfile.objects.get_or_create(user=instance)
