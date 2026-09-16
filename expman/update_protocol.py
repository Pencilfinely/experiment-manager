"""Compatibility with the cooperative stop implemented by the running backend."""

UPDATE_STOP_PROTOCOL = 1

# These published backends implement the protocol but predate its explicit marker.
# Unknown releases must advertise their capability instead of inheriting a guess.
_UNMARKED_RELEASES = frozenset(('0.3.0rc2', '0.3.0rc3', '0.3.0-rc.2', '0.3.0-rc.3'))


def supports_update_stop(state):
    if 'update_stop_protocol' in state:
        protocol = state['update_stop_protocol']
        return type(protocol) is int and protocol == UPDATE_STOP_PROTOCOL
    version = state.get('version')
    return isinstance(version, str) and version in _UNMARKED_RELEASES


def manual_stop_required(state, *, worker):
    version = state.get('version')
    version_text = version if isinstance(version, str) and version else '未知'
    role, action = ('算力端', '停止代理') if worker else ('主控', '停止主控')
    return dict(state, ready_for_update=False, manual_stop_required=True,
                backend_version=version,
                detail=f'当前后台{role}版本 {version_text} 不支持安全更新握手。'
                       f'请确认实验及文件回传完成，在客户端点击「{action}」，然后重试安装更新。'
                       '若由终端启动，请退出原启动终端。')
