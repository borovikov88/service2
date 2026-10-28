from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from pool_service.models import Client, Organization, OrganizationAccess, Pool, PoolAccess
from pool_service.client_crm_models import ClientBusinessRole, ClientCompanyLink, ClientContact, ClientCRMProfile


class ContactsPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org=Organization.objects.create(name="Synthetic paged org",paid_until=timezone.now()+timedelta(days=30))
        cls.other=Organization.objects.create(name="Synthetic foreign org")
        cls.actor=get_user_model().objects.create_user(username="synthetic-pagination-owner")
        OrganizationAccess.objects.create(user=cls.actor,organization=cls.org,role="owner")
        cls.companies=Client.objects.bulk_create([Client(name=f"Company {i:04}",organization=cls.org,client_type="legal") for i in range(251)])
        cls.people=Client.objects.bulk_create([Client(name=f"Person {i:04}",organization=cls.org,client_type="private") for i in range(151)])
        cls.hidden=Client.objects.create(name="Hidden unmatched secret",organization=cls.other,client_type="private")
        ClientBusinessRole.objects.bulk_create([ClientBusinessRole(client=c,role="supplier",active=True,revision=1) for c in cls.companies])
        ClientCompanyLink.objects.create(company=cls.companies[0],person=cls.people[-1],position="Bounded outside page")
        ClientCompanyLink.objects.create(company=cls.companies[0],person=cls.hidden)
        ClientContact.objects.create(client=cls.companies[-1],kind="email",value="last-page@example.test")

    def setUp(self):
        self.client.force_login(self.actor)
        self.url=reverse("clients_list")

    def test_large_scope_only_current_pages_materialized_and_no_hidden_search_dump(self):
        with CaptureQueriesContext(connection) as queries:
            response=self.client.get(self.url)
        self.assertEqual(len(response.context["companies"]),50)
        self.assertEqual(len(response.context["private_contacts"]),50)
        self.assertEqual(len(response.context["suppliers"]),50)
        self.assertEqual(response.context["companies_page"].paginator.count,251)
        self.assertNotContains(response,self.companies[50].name)
        self.assertNotContains(response,self.hidden.name)
        self.assertNotContains(response,'data-search=')
        self.assertTrue(any('LIMIT 50' in q['sql'] for q in queries))
        self.assertEqual(response.context['companies'][0].linked_people[0].person_id,self.people[-1].pk)

    def test_second_page_and_invalid_page_stable_order(self):
        first=self.client.get(self.url)
        second=self.client.get(self.url,{"companies_page":2,"suppliers_page":2})
        self.assertEqual(second.context['companies'][0].pk,self.companies[50].pk)
        self.assertTrue(set(c.pk for c in first.context['companies']).isdisjoint(c.pk for c in second.context['companies']))
        self.assertEqual(second.context['suppliers_page'].paginator.count,251)
        last=self.client.get(self.url,{"companies_page":999999})
        self.assertEqual([c.pk for c in last.context['companies']],[self.companies[-1].pk])
        malformed=self.client.get(self.url,{"companies_page":"invalid"})
        self.assertEqual(malformed.context['companies_page'].number,1)

    def test_search_before_pagination_finds_email_and_related_contact_on_later_page(self):
        response=self.client.get(self.url,{"companies_q":"last-page@example.test","companies_page":2})
        self.assertEqual([c.pk for c in response.context['companies']],[self.companies[-1].pk])
        self.assertEqual(response.context['companies_page'].paginator.count,1)
        related=self.client.get(self.url,{"companies_q":self.people[-1].name})
        self.assertEqual([c.pk for c in related.context['companies']],[self.companies[0].pk])
        hidden=self.client.get(self.url,{"companies_q":self.hidden.name})
        self.assertEqual(hidden.context['companies_page'].paginator.count,0)

    def test_related_payload_bounded_but_search_includes_all_authorized_links(self):
        ClientCompanyLink.objects.bulk_create([ClientCompanyLink(company=self.companies[1],person=p) for p in self.people])
        response=self.client.get(self.url,{"companies_q":self.companies[1].name})
        row=response.context['companies'][0]
        self.assertEqual(len(row.linked_people),20)
        self.assertTrue(row.more_relationships)
        self.assertContains(response,"Все связи в карточке")
        response=self.client.get(self.url,{"companies_q":self.people[-2].name})
        self.assertEqual([c.pk for c in response.context['companies']],[self.companies[1].pk])

    def test_supplier_count_and_search_respect_foreign_merged_and_inactive_roles(self):
        ClientBusinessRole.objects.create(client=self.hidden,role="supplier",active=True)
        ClientCRMProfile.objects.create(client=self.companies[0],merged_into=self.companies[1])
        ClientBusinessRole.objects.filter(client=self.companies[2]).update(active=False)
        response=self.client.get(self.url,{"suppliers_q":"Company"})
        self.assertEqual(response.context['suppliers_page'].paginator.count,249)
        response=self.client.get(self.url,{"suppliers_q":self.hidden.name})
        self.assertEqual(response.context['suppliers_page'].paginator.count,0)

    def test_literal_cyrillic_and_formatted_phone_search(self):
        person=self.people[-1];person.name="Алёна Тестовая";person.phone="+7 (900) 123-45-67";person.save()
        for query in ["алена","9001234567","900 123 45 67"]:
            response=self.client.get(self.url,{"people_q":query})
            self.assertEqual([c.pk for c in response.context['private_contacts']],[person.pk])
        response=self.client.get(self.url,{"people_q":"[.*"})
        self.assertEqual(response.context['people_page'].paginator.count,0)

    def test_russian_trunk_alias_keeps_explicit_foreign_number_separate(self):
        person=self.people[-1];person.phone="8 (900) 123-45-67";person.save()
        foreign_format=self.people[-2];foreign_format.phone=" +8 (900) 123-45-67";foreign_format.save()
        for query in ["79001234567", "+7 (900) 123-45-67"]:
            response=self.client.get(self.url,{"people_q":query})
            self.assertEqual([c.pk for c in response.context['private_contacts']],[person.pk])
            related=self.client.get(self.url,{"companies_q":query})
            self.assertEqual([c.pk for c in related.context['companies']],[self.companies[0].pk])

    def test_filtered_supplier_empty_state_is_not_unclassified_claim(self):
        response=self.client.get(self.url,{"suppliers_q":"No such supplier"})
        self.assertContains(response,"По запросу ничего не найдено")
        self.assertNotContains(response,"Поставщики ещё не отмечены")

    def test_pool_staff_is_separately_paged_scoped_and_filtered(self):
        pools=Pool.objects.bulk_create([Pool(client=self.companies[0],organization=self.org,address=f"Synthetic object {i:03}") for i in range(55)])
        PoolAccess.objects.bulk_create([PoolAccess(pool=p,user=self.actor,role="viewer") for p in pools])
        malformed=Pool.objects.create(client=self.hidden,organization=self.org,address="Foreign hidden object")
        PoolAccess.objects.create(pool=malformed,user=self.actor,role="viewer")
        response=self.client.get(self.url,{"clients_tab":"people","staff_page":2})
        self.assertEqual(len(response.context['pool_staff']),5)
        self.assertEqual(response.context['staff_page'].paginator.count,55)
        self.assertNotContains(response,"Foreign hidden object")
        response=self.client.get(self.url,{"people_q":"object 054"})
        self.assertEqual(response.context['staff_page'].paginator.count,1)
        self.assertEqual(response["Cache-Control"],"private, no-store")

    def test_legacy_directory_normalization_cases_now_run_on_server(self):
        company=self.companies[0];company.name='ООО «СТРОЙ–ИНВЕСТ» Семён';company.phone='+7 900 123 4567';company.save()
        for query in ['Строй инвест','Строй-инвест','  строй   инвест ','СТРОЙ\u00a0ИНВЕСТ','семен','9001234567']:
            response=self.client.get(self.url,{'companies_q':query})
            self.assertEqual([row.pk for row in response.context['companies']],[company.pk])
        response=self.client.get(self.url,{'companies_q':'Несуществующий'})
        self.assertEqual(response.context['companies_page'].paginator.count,0)
