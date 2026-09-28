import secrets

from django.core.management.base import BaseCommand, CommandError

from pool_service.communication_models import ChannelConnection


class Command(BaseCommand):
    help = "Rotate the bearer token used by a website communication connection."

    def add_arguments(self, parser):
        parser.add_argument("connection_id", type=int)

    def handle(self, *args, **options):
        try:
            connection = ChannelConnection.objects.select_related("channel").get(
                pk=options["connection_id"],
                channel__kind="website",
            )
        except ChannelConnection.DoesNotExist as exc:
            raise CommandError("Активное подключение сайта с таким ID не найдено.") from exc
        token = secrets.token_urlsafe(32)
        connection.set_api_token(token)
        connection.save(update_fields=["api_token_hash"])
        self.stdout.write(f"public_id={connection.public_id}")
        self.stdout.write(f"token={token}")
        self.stderr.write("Сохраните токен сейчас: повторно он показан не будет.")
