# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import fixtures
import mock

from oslo_serialization import jsonutils

import nova.compute
from nova import exception
from nova.scheduler import weights
from nova.tests.functional import integrated_helpers
from nova.tests.unit import fake_notifier
from nova.tests.unit.image import fake as fake_image


class HostNameWeigher(weights.BaseHostWeigher):
    # Weigher to make the scheduler alternate host list deterministic
    _weights = {'host1': 100, 'host2': 50, 'host3': 10}

    def _weigh_object(self, host_state, weight_properties):
        # Any undefined host gets no weight.
        return self._weights.get(host_state.host, 0)


class RequestSpecImageSerializationFixture(fixtures.Fixture):
    """A fixture to temporary fix oslo.messaging bug #1529084 by serializing
    datetime objects into strings in legacy dicts.

    It seems that the fake rpc driver does not accept datetimes as
    serializable. So we need to hack around in nova to be able to run test with
    fake RPC.
    """
    def __init__(self, test):
        self.test = test

    def setUp(self):
        super(RequestSpecImageSerializationFixture, self).setUp()
        orig_legacy_image = (
            nova.objects.request_spec.RequestSpec._to_legacy_image)

        def wrap_legacy_image(*args, **kwargs):
            image_p = orig_legacy_image(*args, **kwargs)
            return jsonutils.loads(jsonutils.dumps(image_p))

        self.test.stub_out(
            'nova.objects.request_spec.RequestSpec._to_legacy_image',
            wrap_legacy_image)


class PinnedComputeRpcTests(integrated_helpers.ProviderUsageBaseTestCase):

    compute_driver = 'fake.MediumFakeDriver'

    def setUp(self):
        # Use our custom weigher to make sure that we have
        # a predictable host selection order during scheduling
        self.flags(weight_classes=[__name__ + '.HostNameWeigher'],
                   group='filter_scheduler')

        super(PinnedComputeRpcTests, self).setUp()
        fake_notifier.stub_notifier(self)
        self.addCleanup(fake_notifier.reset)

        self.useFixture(RequestSpecImageSerializationFixture(self))

        self.compute1 = self._start_compute(host='host1')
        self.compute2 = self._start_compute(host='host2')
        self.compute3 = self._start_compute(host='host3')

        flavors = self.api.get_flavors()
        self.flavor1 = flavors[0]

    def _test_reschedule_migration_with_compute_rpc_pin(self, version_cap):
        self.flags(compute=version_cap, group='upgrade_levels')

        server_req = self._build_minimal_create_server_request(
            self.api, 'server1',
            networks=[],
            image_uuid=fake_image.get_valid_image_id(),
            flavor_id=self.flavor1['id'])
        server = self.api.post_server({'server': server_req})
        server = self._wait_for_state_change(self.admin_api, server, 'ACTIVE')

        orig_claim = nova.compute.resource_tracker.ResourceTracker.resize_claim
        claim_calls = []

        def fake_orig_claim(
                _self, context, instance, instance_type, nodename,
                *args, **kwargs):
            if not claim_calls:
                claim_calls.append(nodename)
                raise exception.ComputeResourcesUnavailable(
                    reason='Simulated claim failure')
            else:
                claim_calls.append(nodename)
                return orig_claim(
                    _self, context, instance, instance_type, nodename, *args,
                    **kwargs)

        with mock.patch(
                'nova.compute.resource_tracker.ResourceTracker.resize_claim',
                new=fake_orig_claim):
            # Now migrate the server which is going to fail on the first
            # destination but then will be rescheduled.
            self.api.post_server_action(server['id'], {'migrate': None})

            # We expect that the instance is on host3 as the scheduler
            # selected host2 due to our weigher and the cold migrate failed
            # there and re-scheduled to host3 were it succeeded.
            self._wait_for_server_parameter(
                self.api, server,
                {
                    'OS-EXT-SRV-ATTR:host': 'host3',
                    'OS-EXT-STS:task_state': None,
                    'status': 'VERIFY_RESIZE'})

        # we ensure that there was a failed and then a successful claim call
        self.assertEqual(['host2', 'host3'], claim_calls)

    def test_reschedule_migration_5_1(self):
        self._test_reschedule_migration_with_compute_rpc_pin('5.1')

    def test_reschedule_migration_5_0(self):
        # This should work the same as test_reschedule_migration_5_1 so the
        # commented out call below should pass but it doesn't. This is
        # bug 1843090.
        # self._test_reschedule_migration_with_compute_rpc_pin('5.0')

        # So the above call is inlined below and modified to assert the wrong
        # behavior
        self.flags(compute='5.0', group='upgrade_levels')

        server_req = self._build_minimal_create_server_request(
            self.api, 'server1',
            networks=[],
            image_uuid=fake_image.get_valid_image_id(),
            flavor_id=self.flavor1['id'])
        server = self.api.post_server({'server': server_req})
        server = self._wait_for_state_change(self.admin_api, server, 'ACTIVE')

        orig_claim = nova.compute.resource_tracker.ResourceTracker.resize_claim
        claim_calls = []

        def fake_orig_claim(
                _self, context, instance, instance_type, nodename,
                *args, **kwargs):
            if not claim_calls:
                claim_calls.append(nodename)
                raise exception.ComputeResourcesUnavailable(
                    reason='Simulated claim failure')
            else:
                claim_calls.append(nodename)
                return orig_claim(
                    _self, context, instance, instance_type, nodename, *args,
                    **kwargs)

        with mock.patch(
                'nova.compute.resource_tracker.ResourceTracker.resize_claim',
                new=fake_orig_claim):
            # Now migrate the server which is going to fail on the first
            # destination but then will be rescheduled.
            self.api.post_server_action(server['id'], {'migrate': None})

            # bug 1843090: The migration failed and the instance remained on
            # the source host
            server = self._wait_for_server_parameter(
                self.api, server,
                {
                    'OS-EXT-SRV-ATTR:host': 'host1',
                    'OS-EXT-STS:task_state': None,
                    'status': 'ERROR'})

        # there was only one resize_claim call as no reschedule happened
        self.assertEqual(['host2'], claim_calls)

        # bug 1843090: The following stack trace is printed in the log when the
        # re-schedule is initiated:
        #
        #     Traceback (most recent call last):
        #   [snip]
        #       File "nova/conductor/manager.py", line 95, in wrapper
        #         return fn(self, context, *args, **kwargs)
        #       File "nova/compute/utils.py", line 1372, in decorated_function
        #         return function(self, context, *args, **kwargs)
        #       File "nova/conductor/manager.py", line 299, in migrate_server
        #         host_list)
        #       File "nova/conductor/manager.py", line 327, in _cold_migrate
        #         request_spec.flavor = flavor
        #     AttributeError: 'dict' object has no attribute 'flavor'
        #
        self.assertIn(
            "AttributeError: 'dict' object has no attribute 'flavor'",
            self.stdlog.logger.output)
