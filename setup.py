import os
import re

import enums
from aws_login import login_to_aws
from cli import mara, utils, workspace


def _compose_project_name():
    """Replicate docker compose's default project name — the deploy folder's
    name lowercased with invalid characters stripped — honoring a
    ``COMPOSE_PROJECT_NAME`` override in the root .env, so generated volume
    names match the ones compose actually creates.
    """
    compose_env = utils.read_env_file(enums.COMPOSE_ENV_FILE)
    name = compose_env.get('COMPOSE_PROJECT_NAME') or os.path.basename(
        os.path.dirname(os.path.abspath(__file__))
    )
    return re.sub(r'[^a-z0-9_-]', '', name.lower()).lstrip('_-')


COMPOSE_PROJECT_NAME = _compose_project_name()
EMBEDDINGS_VOLUME = f'{COMPOSE_PROJECT_NAME}_mara-embeddings-volume'

# Compose profiles gating each service group in the compose files. nginx-proxy
# carries no profile so it always runs; `local-embeddings` additionally gates
# the bundled mara-hf-tei service.
WEB_PROFILE = 'web'
WORKSPACES_PROFILE = 'workspaces'

MODES = {
    '1': ('Everything (workspaces + web)', {WORKSPACES_PROFILE, WEB_PROFILE}),
    '2': ('Web only (MARA)', {WEB_PROFILE}),
    '3': ('Workspaces only', {WORKSPACES_PROFILE}),
}

SERVICE_DESCRIPTIONS = {
    WORKSPACES_PROFILE: (
        " - Workspace API: The main API for Nanome v2 workspaces.\n"
        " - Workspace DB: The database for the workspace API.\n"
        " - Nanome auth proxy: Proxy for Nanome authentication used by VR headsets.\n"
    ),
    WEB_PROFILE: (
        " - Web UI: Web Application and API for managing workspaces and performing comp-chem workflows.\n"
        " - Tool Server: Runs computations for MARA workflows.\n"
        " - Embeddings: Vector database for storing embeddings.\n"
    ),
}


def _read_compose_profiles():
    compose_env = utils.read_env_file(enums.COMPOSE_ENV_FILE)
    return {p for p in compose_env.get('COMPOSE_PROFILES', '').split(',') if p}


def _default_mode(old_profiles):
    """Preselect the mode matching the profiles from the previous run."""
    has_web = WEB_PROFILE in old_profiles
    has_workspaces = WORKSPACES_PROFILE in old_profiles
    if has_web and not has_workspaces:
        return 2
    if has_workspaces and not has_web:
        return 3
    return 1


def _default_host():
    """Prefill the host from whichever env file a previous run left behind."""
    mara_env = utils.read_env_file(enums.MARA_ENV_FILE)
    host = utils.extract_host_from_env(mara_env, 'nanome')
    if host:
        return host
    workspaces_env = utils.read_env_file(enums.WORKSPACES_ENV_FILE)
    return utils.extract_host_from_env(workspaces_env, 'workspaces')


def _write_compose_env(profiles, mara_env=None):
    """Manage the root .env that ``docker compose`` reads.

    ``COMPOSE_PROFILES`` records which service groups this deployment runs
    (web / workspaces), plus ``local-embeddings`` when the bundled
    ``mara-hf-tei`` service is in use — the marker for that is
    ``EMBEDDING_API_URL``, which only the local-embeddings branch sets.
    ``TEI_MODEL_ID`` forwards the chosen HF model id so the TEI container
    loads the same one mara-ai will query. Returns the full new profile set.
    """
    compose_env = utils.read_env_file(enums.COMPOSE_ENV_FILE)
    profiles = set(profiles)
    mara_env = mara_env or {}
    uses_local_embeddings = (mara_env.get('EMBEDDING_API_URL') or '').startswith(
        'http://mara-hf-tei'
    )
    if uses_local_embeddings:
        profiles.add('local-embeddings')
        compose_env['TEI_MODEL_ID'] = mara_env.get('EMBEDDING_MODEL', '')
    else:
        compose_env.pop('TEI_MODEL_ID', None)
    compose_env['COMPOSE_PROFILES'] = ','.join(sorted(profiles))
    utils.write_env_file(enums.COMPOSE_ENV_FILE, compose_env)
    return profiles


def _warn_if_profiles_removed(old_profiles, new_profiles):
    """``docker compose up`` ignores services from disabled profiles but does
    not stop their running containers — the operator must remove them.
    """
    removed = old_profiles - new_profiles
    if not removed:
        return
    print(
        f"\n!! This deployment no longer includes: {', '.join(sorted(removed))}\n"
        "   Containers from the previous configuration will keep running.\n"
        "   Before `docker compose up -d --wait`, remove them with:\n"
        "     docker compose down --remove-orphans\n"
    )


def _warn_if_embedding_model_changed(old, new):
    """Chroma persists vectors in a model-specific space and dimensionality.
    Switching ``EMBEDDING_MODEL`` makes existing vectors incompatible, the
    operator must drop the volume before bringing the stack back up.
    """
    if not old or not new or old == new:
        return
    print(
        f"\n!! EMBEDDING_MODEL changed: {old} -> {new}\n"
        "   Persisted embedding vectors are incompatible with the new model.\n"
        "   Before `docker compose up -d --wait`, run:\n"
        "     docker compose down\n"
        f"     docker volume rm {EMBEDDINGS_VOLUME}\n"
    )


def setup_workspaces(host, cert_type, mara_url=None):
    """Configure the workspaces service group: Workspace API and auth proxy.

    ``mara_url`` is only set when the web group is part of the same deployment.
    """
    workspaces_env = workspace.configure_workspace_api()
    workspaces_env['VIRTUAL_HOST'] = f'workspaces.{host}'
    if cert_type == 'Default':
        workspaces_env['CERT_NAME'] = 'default'
    elif cert_type == 'Individual':
        workspaces_env['CERT_NAME'] = 'workspaces'
    if mara_url:
        workspaces_env['MARA_URL'] = mara_url
    utils.write_env_file(enums.WORKSPACES_ENV_FILE, workspaces_env)

    nanome_auth_env = {'VIRTUAL_HOST': f'nanome-auth.{host}'}
    if cert_type == 'Default':
        nanome_auth_env['CERT_NAME'] = 'default'
    elif cert_type == 'Individual':
        nanome_auth_env['CERT_NAME'] = 'nanome-auth'
    utils.write_env_file(enums.AUTH_PROXY_ENV_FILE, nanome_auth_env)

    return workspaces_env, nanome_auth_env


def setup_mara(host, cert_type, workspace_api_url=None):
    """Configure the web service group: MARA, tool server, and embeddings.

    ``workspace_api_url`` is only set when the workspaces group is part of the
    same deployment; otherwise any stale value is removed from the env.
    """
    protocol = 'http' if cert_type == 'None' else 'https'

    # Setup tool-server .env file
    tool_server_host = f'nanome-tools.{host}'
    existing_tool_server_env = utils.read_env_file(enums.TOOL_SERVER_ENV_FILE)
    tool_server_env = mara.configure_tool_server(existing_tool_server_env)
    tool_server_env.update(
        FILES_VOLUME=f'{COMPOSE_PROJECT_NAME}_mara-tool-volume',
        VIRTUAL_HOST=tool_server_host,
    )
    if cert_type == 'Default':
        tool_server_env.update(
            CERT_NAME='default',
            REQUESTS_CA_BUNDLE='/certs/bundle.pem',
        )
    elif cert_type == 'Individual':
        tool_server_env.update(
            CERT_NAME='nanome-tools',
            REQUESTS_CA_BUNDLE='/certs/bundle.pem',
        )
    else:
        tool_server_env.pop('CERT_NAME', None)
        tool_server_env.pop('REQUESTS_CA_BUNDLE', None)

    # Configure the mara .env file
    tool_server_api_key = tool_server_env.get('API_KEY', None)
    mara_host = f'nanome.{host}'
    existing_mara_env = utils.read_env_file(enums.MARA_ENV_FILE)
    mara_env = mara.configure_mara_server(existing_mara_env)

    _warn_if_embedding_model_changed(
        old=existing_mara_env.get('EMBEDDING_MODEL'),
        new=mara_env.get('EMBEDDING_MODEL'),
    )

    mara_env.update(
        API_HOST=mara_host,
        TOOL_SERVER_KEY=tool_server_api_key,
        TOOL_SERVER_URL=f'{protocol}://{tool_server_host}',
        VIRTUAL_HOST=mara_host,
    )
    if workspace_api_url:
        mara_env['WORKSPACE_API_URL'] = workspace_api_url
    else:
        mara_env.pop('WORKSPACE_API_URL', None)
    if cert_type == 'Default':
        mara_env.update(
            CERT_NAME='default',
            REQUESTS_CA_BUNDLE='/certs/bundle.pem',
        )
    elif cert_type == 'Individual':
        mara_env.update(
            CERT_NAME='nanome',
            REQUESTS_CA_BUNDLE='/certs/bundle.pem',
        )
    else:
        mara_env.pop('CERT_NAME', None)
        mara_env.pop('REQUESTS_CA_BUNDLE', None)

    # The tool server downloads repo tarballs from the same GitHub host when
    # building images for tools imported from GitHub.
    if mara_env.get('GITHUB_BASE_URL'):
        tool_server_env['GITHUB_BASE_URL'] = mara_env['GITHUB_BASE_URL']
    else:
        tool_server_env.pop('GITHUB_BASE_URL', None)

    utils.write_env_file(enums.TOOL_SERVER_ENV_FILE, tool_server_env)
    utils.write_env_file(enums.MARA_ENV_FILE, mara_env)
    return mara_env, tool_server_env


def setup_nanome_ai():
    old_profiles = _read_compose_profiles()

    print("\nThanks for using Nanome! Let's get started setting up your server!")
    mode = utils.ask_selection(
        'What would you like to deploy?',
        [label for label, _ in MODES.values()],
        default=_default_mode(old_profiles),
    )
    profiles = MODES[mode][1]

    descriptions = ''.join(
        SERVICE_DESCRIPTIONS[p]
        for p in (WORKSPACES_PROFILE, WEB_PROFILE)
        if p in profiles
    )
    print(
        '\nThis script will set up the environment variables '
        'for the following docker containers:\n' + descriptions
    )
    input('Press ENTER to continue')

    host = utils.gather_host_info(default=_default_host())
    cert_type = utils.gather_https_info(host)
    protocol = 'http' if cert_type == 'None' else 'https'

    service_urls = []
    mara_env = None

    if WORKSPACES_PROFILE in profiles:
        mara_url = (
            f'{protocol}://nanome.{host}' if WEB_PROFILE in profiles else None
        )
        workspaces_env, nanome_auth_env = setup_workspaces(host, cert_type, mara_url)
        service_urls += [
            ('Workspace API', workspaces_env['VIRTUAL_HOST']),
            ('Nanome Auth Proxy', nanome_auth_env['VIRTUAL_HOST']),
        ]

    if WEB_PROFILE in profiles:
        workspace_api_url = (
            f'{protocol}://workspaces.{host}'
            if WORKSPACES_PROFILE in profiles
            else None
        )
        mara_env, tool_server_env = setup_mara(host, cert_type, workspace_api_url)
        service_urls = [
            ('Web UI', mara_env['VIRTUAL_HOST']),
            ('Tool Server', tool_server_env['VIRTUAL_HOST']),
        ] + service_urls

    new_profiles = _write_compose_env(profiles, mara_env)
    _warn_if_profiles_removed(old_profiles, new_profiles)

    return service_urls


if __name__ == "__main__":
    try:
        service_urls = setup_nanome_ai()
        login_to_aws()

        url_lines = ''.join(f' - {name}: {host}\n' for name, host in service_urls)
        print(
            "\nYour services have been configured to run at the following urls\n"
            + url_lines
            + "\nTo start the services, run `docker compose up -d --wait`"
        )
    except KeyboardInterrupt:
        print("\nSetup cancelled")
