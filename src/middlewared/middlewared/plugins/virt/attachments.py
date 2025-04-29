from itertools import product
from typing import TYPE_CHECKING

from middlewared.common.attachment import FSAttachmentDelegate
from middlewared.common.ports import PortDelegate

from .utils import Status

if TYPE_CHECKING:
    from middlewared.main import Middleware


class VirtFSAttachmentDelegate(FSAttachmentDelegate):

    name = 'virt'
    title = 'Virtualization'

    async def query(self, path, enabled, options=None):
        virt_config = await self.middleware.call('virt.global.config')
        if not virt_config['pool']:
            return []

        instances = []
        pool = path.split('/')[2] if path.count('/') == 2 else None  # only set if path is pool mp
        dataset = path.removeprefix('/mnt/')
        incus_pool_change = dataset in virt_config['storage_pools'] or dataset == virt_config['pool']
        for i in await self.middleware.call('virt.instance.query'):
            append = False
            if pool and i['storage_pool'] == pool:
                instances.append({
                    'id': i['id'],
                    'name': i['name'],
                    'disk_devices': [],
                    'dataset': dataset,
                })
                continue

            disks = []
            for device in await self.middleware.call('virt.instance.device_list', i['id']):
                if device['dev_type'] != 'DISK':
                    continue

                if pool and device['storage_pool'] == pool:
                    append = True
                    disks.append(device['name'])
                    continue

                if device['source'] is None:
                    continue

                source_path = device['source'].removeprefix('/dev/zvol/').removeprefix('/mnt/')
                if await self.middleware.call('filesystem.is_child', source_path, dataset):
                    append = True
                    disks.append(device['name'])
                    continue

            if append:
                instances.append({
                    'id': i['id'],
                    'name': i['name'],
                    'disk_devices': disks,
                    'dataset': dataset,
                })

        return [{
            'id': dataset,
            'instances': instances,
            'incus_pool_change': incus_pool_change,
        }] if incus_pool_change or instances else []

    async def delete(self, attachments):
        if not attachments:
            return

        attachment = attachments[0]
        virt_config = await self.middleware.call('virt.global.config')
        storage_pools = [p for p in virt_config['storage_pools'] if p != attachment['id']]
        if attachment['incus_pool_change'] and attachment['id'] == virt_config['pool']:
            # We are exporting main virt pool and at this point we should just unset
            # the pool
            await (await self.middleware.call('virt.global.update', {
                'pool': None,
                'storage_pools': storage_pools,
            })).wait(raise_error=True)
            return

        disks_to_remove = [i for i in filter(lambda i: i.get('disk_devices'), attachments)]
        for instance_data in disks_to_remove:
            for to_remove_disk in instance_data['disk_devices']:
                await self.middleware.call('virt.instance.device_delete', instance_data['name'], to_remove_disk)

        if attachment['incus_pool_change']:
            # This means one of the storage pool is being exported
            new_config = {
                'pool': None,
                'storage_pools': [
                    pool for pool in virt_config['storage_pools']
                    if pool != attachment['id']
                ]
            }
            await (await self.middleware.call('virt.global.update', new_config)).wait(raise_error=True)
            await (await self.middleware.call(
                'virt.global.update', {'pool': virt_config['pool']}
            )).wait(raise_error=True)

    async def toggle(self, attachments, enabled):
        await getattr(self, 'start' if enabled else 'stop')(attachments)

    async def start(self, attachments):
        if not attachments:
            return

        attachment = attachments[0]
        if attachments['incus_pool_change']:
            try:
                await (await self.middleware.call('virt.global.setup')).wait(raise_error=True)
            except Exception:
                self.middleware.logger.error('Failed to start incus')
                # No need to attempt to toggle instances, it won't happen either ways because none could be
                # queried to be started as incus wasn't even running but better safe than sorry
                return

        await self.start_instances(attachment['instances'])

    async def stop(self, attachments):
        if not attachments:
            return

        attachment = attachments[0]
        await self.stop_instances(attachment['instances'])
        if attachment['incus_pool_change']:
            try:
                await self.middleware.call('service.stop', self.service)
            except Exception as e:
                self.middleware.logger.error('Failed to stop incus: %s', e)
            finally:
                await self.middleware.call('virt.global.set_status', Status.LOCKED)

    async def toggle_instances(self, attachments, enabled):
        for attachment in attachments:
            action = 'start' if enabled else 'stop'
            try:
                job = await self.middleware.call(f'virt.instance.{action}', attachment['id'])
                await job.wait(raise_error=True)
            except Exception as e:
                self.middleware.logger.warning('Unable to %s %r: %s', action, attachment['id'], e)

    async def stop_instances(self, attachments):
        await self.toggle_instances(attachments, False)

    async def start_instances(self, attachments):
        await self.toggle_instances(attachments, True)


class VirtPortDelegate(PortDelegate):

    name = 'virt instances'
    namespace = 'virt'
    title = 'Virtualization Device'

    async def get_ports(self):
        ports = []
        for instance_id, instance_ports in (await self.middleware.call('virt.instance.get_ports_mapping')).items():
            if instance_ports := list(product(['0.0.0.0', '::'], instance_ports)):
                ports.append({
                    'description': f'{instance_id!r} instance',
                    'ports': instance_ports,
                    'instance': instance_id,
                })
        return ports


async def setup(middleware: 'Middleware'):
    middleware.create_task(
        middleware.call(
            'pool.dataset.register_attachment_delegate',
            VirtFSAttachmentDelegate(middleware),
        )
    )
    await middleware.call('port.register_attachment_delegate', VirtPortDelegate(middleware))
