import base64
import json
import os
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

from pool_service.models import (
    CashCount,
    Client,
    CrmItem,
    DevelopmentTask,
    Notification,
    Organization,
    OrganizationAccess,
    OrganizationPaymentRequest,
    Pool,
    ServiceTask,
    WaterReading,
)


class VisualDesktopQaTests(StaticLiveServerTestCase):
    host = "127.0.0.1"

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="Аквалайн — визуальный QA",
            plan_type=Organization.PLAN_COMPANY_PAID,
            paid_until=timezone.now() + timedelta(days=60),
        )
        cls.owner = User.objects.create_user(
            username="visual_owner",
            password="visual-pass-123",
            first_name="Александр",
            last_name="QA",
        )
        cls.superuser = User.objects.create_superuser(
            username="visual_superuser",
            password="visual-pass-123",
            email="visual@example.test",
        )
        OrganizationAccess.objects.create(
            user=cls.owner,
            organization=cls.organization,
            role="owner",
        )
        OrganizationAccess.objects.create(
            user=cls.superuser,
            organization=cls.organization,
            role="owner",
        )

        cls.staff = []
        roles = ["manager", "service", "installer", "accountant", "manager", "service"]
        for index, role in enumerate(roles, start=1):
            user = User.objects.create_user(
                username=f"visual_staff_{index}",
                password="visual-pass-123",
                first_name=f"Сотрудник {index}",
                last_name="Аквалайн",
            )
            OrganizationAccess.objects.create(
                user=user,
                organization=cls.organization,
                role=role,
            )
            cls.staff.append(user)

        cls.clients = []
        for index in range(1, 9):
            client = Client.objects.create(
                organization=cls.organization,
                name=f"Клиент для визуальной проверки {index}",
                client_type="private" if index % 2 else "legal",
                phone=f"+7 900 000 0{index:03d}",
                email=f"client{index}@example.test",
            )
            cls.clients.append(client)

        cls.pool = Pool.objects.create(
            organization=cls.organization,
            client=cls.clients[0],
            address="Барнаул, проспект Ленина, 100 — тестовый объект с длинным адресом",
            service_frequency=Pool.SERVICE_FREQ_WEEKLY,
            service_monthly_price="25000.00",
            service_details_comment="Регулярное обслуживание, контроль оборудования и качества воды.",
        )
        WaterReading.objects.create(
            pool=cls.pool,
            date=timezone.now() - timedelta(hours=6),
            added_by=cls.staff[1],
            temperature=27.4,
            ph=7.2,
            cl_free=0.6,
            cl_total=0.8,
            comment="Вода прозрачная, оборудование работает штатно.",
            required_materials="Хлор, pH-минус",
            performed_works="Промывка фильтра, проверка насоса",
        )

        cls.crm_item = CrmItem.objects.create(
            organization=cls.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title="Проверить насос и подготовить предложение по замене оборудования",
            client=cls.clients[0],
            pool=cls.pool,
            stage=CrmItem.STAGE_SERVICE_IN_PROGRESS,
            urgency=CrmItem.URGENCY_REQUIRED,
            description="Длинное описание для проверки поведения карточки на широком экране.",
            responsible=cls.staff[0],
            created_by=cls.owner,
        )
        for index in range(2, 11):
            CrmItem.objects.create(
                organization=cls.organization,
                direction=CrmItem.DIRECTION_SERVICE,
                title=f"Сервисная задача клиента {index}: проверить оборудование",
                client=cls.clients[(index - 1) % len(cls.clients)],
                stage=CrmItem.STAGE_SERVICE_NEW,
                urgency=CrmItem.URGENCY_REQUIRED if index % 3 == 0 else CrmItem.URGENCY_LOW,
                responsible=cls.staff[index % len(cls.staff)],
                created_by=cls.owner,
            )

        today = timezone.localdate()
        for index in range(10):
            task = ServiceTask.objects.create(
                organization=cls.organization,
                title=f"Задача календаря {index + 1} с достаточно длинным названием",
                description="Проверка плотности календаря на desktop.",
                start_date=today + timedelta(days=index % 4),
                end_date=today + timedelta(days=index % 4),
                task_type=ServiceTask.TYPE_MANUAL,
                source_type=ServiceTask.SOURCE_MANAGER,
                status=ServiceTask.STATUS_IN_PROGRESS if index % 3 == 0 else ServiceTask.STATUS_NEW,
                priority=ServiceTask.PRIORITY_HIGH if index % 4 == 0 else ServiceTask.PRIORITY_NORMAL,
                pool=cls.pool,
                client=cls.clients[0],
                created_by=cls.owner,
                primary_responsible=cls.staff[index % len(cls.staff)],
            )
            task.responsibles.add(cls.staff[index % len(cls.staff)])

        cls.dev_task = DevelopmentTask.objects.create(
            organization=cls.organization,
            title="Проверить desktop-интерфейс Service2 после серии редизайнов",
            description="Визуальная проверка ключевых рабочих экранов на 1920 и 2560 пикселях.",
            business_goal="Найти переполнения, слишком крупные карточки и неэффективное использование ширины.",
            definition_of_done="Ключевые страницы выглядят компактно и не имеют горизонтального overflow.",
            priority=DevelopmentTask.PRIORITY_HIGH,
            status=DevelopmentTask.STATUS_TESTING,
            current_stage=DevelopmentTask.STAGE_TESTING,
            initiator=cls.superuser,
        )
        for index in range(5):
            DevelopmentTask.objects.create(
                organization=cls.organization,
                title=f"Дополнительная задача разработки {index + 1}",
                description="Запись для заполнения таблицы.",
                priority=DevelopmentTask.PRIORITY_MEDIUM,
                status=DevelopmentTask.STATUS_NEW,
                initiator=cls.superuser,
            )

        for index in range(6):
            Notification.objects.create(
                user=cls.owner,
                organization=cls.organization,
                pool=cls.pool,
                kind="limits",
                level="warning" if index % 2 else "info",
                title=f"Уведомление {index + 1}",
                message="Проверка компактности списка уведомлений на широком экране.",
            )

        OrganizationPaymentRequest.objects.create(
            organization=cls.organization,
            requested_by=cls.owner,
            months=6,
            status=OrganizationPaymentRequest.STATUS_PENDING,
            note="Визуальная проверка billing admin",
        )

        CashCount.objects.create(
            organization=cls.organization,
            cashbox_type=CashCount.CASHBOX_KKM,
            counted_by=cls.owner,
            occurred_on=date.today(),
            total="125430.00",
            denominations={},
        )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setUpTestData()
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--force-device-scale-factor=1")
        options.add_argument("--hide-scrollbars")
        cls.browser = webdriver.Chrome(options=options)
        cls.browser.set_page_load_timeout(30)
        cls.output_dir = os.environ.get("VISUAL_QA_OUTPUT", "visual-qa")
        os.makedirs(cls.output_dir, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.quit()
        finally:
            super().tearDownClass()

    def _session_cookie(self, user):
        client = DjangoClient()
        client.force_login(user)
        return client.cookies["sessionid"].value

    def _authenticate(self, user):
        self.browser.delete_all_cookies()
        self.browser.get(self.live_server_url + "/")
        self.browser.add_cookie(
            {
                "name": "sessionid",
                "value": self._session_cookie(user),
                "path": "/",
            }
        )

    def _capture(self, name, path, width, height, expected_text, user):
        self._authenticate(user)
        self.browser.set_window_size(width, height)
        self.browser.get(self.live_server_url + path)
        WebDriverWait(self.browser, 20).until(
            lambda driver: driver.execute_script("return document.readyState") == "complete"
        )
        WebDriverWait(self.browser, 10).until(
            lambda driver: expected_text in driver.find_element("tag name", "body").text
        )

        metrics = self.browser.execute_script(
            """
            const root = document.documentElement;
            const body = document.body;
            const offenders = [];
            for (const el of document.querySelectorAll('body *')) {
              const style = getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') continue;
              const r = el.getBoundingClientRect();
              if (r.width < 1 || r.height < 1) continue;
              if (r.right > window.innerWidth + 2 || r.left < -2) {
                offenders.push({
                  tag: el.tagName,
                  id: el.id || '',
                  cls: String(el.className || '').slice(0, 180),
                  left: Math.round(r.left),
                  right: Math.round(r.right),
                  width: Math.round(r.width),
                  text: String(el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 120),
                });
              }
              if (offenders.length >= 30) break;
            }
            return {
              viewportWidth: window.innerWidth,
              viewportHeight: window.innerHeight,
              documentWidth: Math.max(root.scrollWidth, body.scrollWidth),
              documentHeight: Math.max(root.scrollHeight, body.scrollHeight),
              horizontalOverflow: Math.max(root.scrollWidth, body.scrollWidth) - window.innerWidth,
              offenders,
            };
            """
        )
        metrics["page"] = name
        metrics["url"] = path
        metrics["requestedWidth"] = width
        metrics["requestedHeight"] = height

        result = self.browser.execute_cdp_cmd(
            "Page.captureScreenshot",
            {
                "format": "png",
                "captureBeyondViewport": True,
                "fromSurface": True,
            },
        )
        screenshot_path = os.path.join(self.output_dir, f"{name}-{width}.png")
        with open(screenshot_path, "wb") as fh:
            fh.write(base64.b64decode(result["data"]))
        return metrics

    def test_capture_desktop_pages(self):
        pages = [
            ("pool-detail", reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}), self.clients[0].name, self.owner),
            ("pool-service-details", reverse("pool_service_details", kwargs={"pool_uuid": self.pool.uuid}), "Стоимость обслуживания", self.owner),
            ("new-reading", reverse("water_reading_create", kwargs={"pool_uuid": self.pool.uuid}), self.clients[0].name, self.owner),
            ("kkm", reverse("finance_kkm_cash_dashboard"), "Касса ККМ", self.owner),
            ("finance-data", reverse("finance_data"), "Данные 1С", self.owner),
            ("finance-report", reverse("finance_report"), "Отчёт по расходам", self.owner),
            ("crm-index", reverse("crm_index"), "CRM", self.owner),
            ("crm-service", reverse("crm_list", kwargs={"direction": "service"}), "Сервис", self.owner),
            ("crm-detail", reverse("crm_view", kwargs={"direction": "service", "item_id": self.crm_item.id}), self.crm_item.title, self.owner),
            ("clients", reverse("clients_list"), "Клиенты", self.owner),
            ("users", reverse("users"), "Пользователи", self.owner),
            ("notifications", reverse("notifications"), "Уведомления", self.owner),
            ("calendar", reverse("readings_all"), "План", self.owner),
            ("development-list", reverse("development_task_list"), "Задачи разработки", self.superuser),
            ("development-detail", reverse("development_task_detail", kwargs={"task_id": self.dev_task.id}), self.dev_task.title, self.superuser),
            ("billing-admin", reverse("billing_admin"), "Заявки на продление", self.superuser),
        ]
        metrics = []
        for width, height in [(1920, 1080), (2560, 1440)]:
            for name, path, expected_text, user in pages:
                metrics.append(
                    self._capture(name, path, width, height, expected_text, user)
                )

        with open(os.path.join(self.output_dir, "metrics.json"), "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, ensure_ascii=False, indent=2)

        severe_overflow = [
            item
            for item in metrics
            if item["horizontalOverflow"] > 24
        ]
        if severe_overflow:
            summary = ", ".join(
                f"{item['page']}@{item['requestedWidth']}={item['horizontalOverflow']}px"
                for item in severe_overflow
            )
            self.fail(f"Detected severe horizontal overflow: {summary}")
