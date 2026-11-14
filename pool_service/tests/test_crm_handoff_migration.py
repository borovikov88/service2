from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from pool_service.tests.test_crm_handoffs import HandoffFixture
from pool_service.models import ServiceTask, CrmItem
from pool_service.crm_quote_models import CrmQuote, CrmQuoteEvent


class HandoffMigrationTests(HandoffFixture,TransactionTestCase):
    def test_additive_tables_preserve_existing_quote_request_tasks(self):
        models=(ServiceTask,CrmItem,CrmQuote,CrmQuoteEvent)
        snapshots={m.__name__:list(m.objects.order_by('pk').values()) for m in models}
        executor=MigrationExecutor(connection)
        executor.migrate([('pool_service','0163_crm_quote_lifecycle')])
        try:
            executor=MigrationExecutor(connection)
            target=[('pool_service','0164_crm_service_handoff')]
            executor.migrate(target)
            apps=executor.loader.project_state(target).apps
            for name,values in snapshots.items():
                self.assertEqual(list(apps.get_model('pool_service',name).objects.order_by('pk').values()),values)
            for name in ('CrmHandoff','CrmHandoffTask','CrmHandoffEvent'):
                self.assertEqual(apps.get_model('pool_service',name).objects.count(),0)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
