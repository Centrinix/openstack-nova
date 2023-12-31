#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from unittest import mock

from nova import objects
from nova.tests import fixtures
from nova.tests.functional.notification_sample_tests \
    import notification_sample_base


class TestComputeTaskNotificationSample(
        notification_sample_base.NotificationSampleTestBase):

    def setUp(self):
        super(TestComputeTaskNotificationSample, self).setUp()
        self.neutron = fixtures.NeutronFixture(self)
        self.useFixture(self.neutron)

    def test_build_instances_fault(self):
        # Force down the compute node
        service_id = self.api.get_service_id('nova-compute')
        self.admin_api.put_service_force_down(service_id, True)

        server = self._boot_a_server(
            expected_status='ERROR',
            extra_params={'networks': [{'port': self.neutron.port_1['id']}]},
            additional_extra_specs={'hw:numa_nodes': 1,
                                    'hw:numa_cpus.0': '0',
                                    'hw:numa_mem.0': 512})
        self._wait_for_notification('compute_task.build_instances.error')
        # 0. scheduler.select_destinations.start
        # 1. compute_task.rebuild_server.error
        self.assertEqual(2, len(self.notifier.versioned_notifications),
                         self.notifier.versioned_notifications)
        self._verify_notification(
            'compute_task-build_instances-error',
            replacements={
                'instance_uuid': server['id'],
                'request_spec.instance_uuid': server['id'],
                'request_spec.security_groups': [],
                'request_spec.numa_topology.instance_uuid': server['id'],
                'request_spec.pci_requests.instance_uuid': server['id'],
                'reason.function_name': self.ANY,
                'reason.module_name': self.ANY,
                'reason.traceback': self.ANY
            },
            actual=self.notifier.versioned_notifications[1])

    @mock.patch.object(
        objects.service, 'get_minimum_version_all_cells',
        new=mock.Mock(return_value=62)
    )
    def test_rebuild_fault(self):
        server = self._boot_a_server(
            extra_params={'networks': [{'port': self.neutron.port_1['id']}]},
            additional_extra_specs={'hw:numa_nodes': 1,
                                    'hw:numa_cpus.0': '0',
                                    'hw:numa_mem.0': 512})
        self._wait_for_notification('instance.create.end')
        # Force down the compute node
        service_id = self.api.get_service_id('nova-compute')
        self.admin_api.put_service_force_down(service_id, True)

        self.notifier.reset()

        # NOTE(takashin): The rebuild action and the evacuate action shares
        # same code path. So the 'evacuate' action is used for this test.
        self._evacuate_server(
            server, expected_state='ERROR', expected_migration_status='error')

        self._wait_for_notification('compute_task.rebuild_server.error')
        # 0. instance.evacuate
        # 1. scheduler.select_destinations.start
        # 2. compute_task.rebuild_server.error
        self.assertEqual(3, len(self.notifier.versioned_notifications),
                         self.notifier.versioned_notifications)
        self._verify_notification(
            'compute_task-rebuild_server-error',
            replacements={
                'instance_uuid': server['id'],
                'request_spec.instance_uuid': server['id'],
                'request_spec.security_groups': [],
                'request_spec.numa_topology.instance_uuid': server['id'],
                'request_spec.pci_requests.instance_uuid': server['id'],
                'reason.function_name': self.ANY,
                'reason.module_name': self.ANY,
                'reason.traceback': self.ANY
            },
            actual=self.notifier.versioned_notifications[2])

    def test_migrate_fault(self):
        server = self._boot_a_server(
            extra_params={'networks': [{'port': self.neutron.port_1['id']}]},
            additional_extra_specs={'hw:numa_nodes': 1,
                                    'hw:numa_cpus.0': '0',
                                    'hw:numa_mem.0': 512})
        self._wait_for_notification('instance.create.end')
        # Disable the compute node
        service_id = self.api.get_service_id('nova-compute')
        self.admin_api.put_service(service_id, {'status': 'disabled'})

        self.notifier.reset()

        # Note that the operation will return a 202 response but fail with
        # NoValidHost asynchronously.
        self.admin_api.post_server_action(server['id'], {'migrate': None})
        self._wait_for_notification('compute_task.migrate_server.error')
        self.assertEqual(1, len(self.notifier.versioned_notifications),
                         self.notifier.versioned_notifications)
        self._verify_notification(
            'compute_task-migrate_server-error',
            replacements={
                'instance_uuid': server['id'],
                'request_spec.instance_uuid': server['id'],
                'request_spec.security_groups': [],
                'request_spec.numa_topology.instance_uuid': server['id'],
                'request_spec.pci_requests.instance_uuid': server['id'],
                'reason.exception_message': 'No valid host was found. ',
                'reason.function_name': self.ANY,
                'reason.module_name': self.ANY,
                'reason.traceback': self.ANY
            },
            actual=self.notifier.versioned_notifications[0])
