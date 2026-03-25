from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import Profile, WaterReading
from .services.notifications import notify_reading_out_of_range

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)


@receiver(post_save, sender=WaterReading)
def notify_out_of_range_on_reading_save(sender, instance, **kwargs):
    notify_reading_out_of_range(instance)

#@receiver(post_save, sender=User)
#def save_user_profile(sender, instance, **kwargs):
#    instance.profile.save()
