import json

from .utils import ask_selection, generate_random_password, masked_input


# Keys this file is responsible for. Anything here gets cleared from the env on
# each run before the new selections are written, so switching providers (e.g.
# OpenAI -> Anthropic, or Azure api_key -> service_principal) doesn't leave
# stale settings behind.
PROVIDER_ENV_KEYS = (
    'OPENAI_API_TYPE',
    'LLM_API_URL',
    'LLM_API_KEY',
    'LLM_API_HEADERS',
    'LLM_MODEL',
    'LLM_MODEL_MD',
    'LLM_MODEL_SM',
    'EMBEDDING_MODEL',
    'EMBEDDING_API_URL',
    'EMBEDDING_API_KEY',
    'VOYAGE_API_KEY',
    'VOYAGE_API_URL',
    'AWS_AUTH_MODE',
    'AWS_REGION',
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN',
    'AZURE_LLM_API_VERSION',
    'AZURE_EMBEDDING_API_VERSION',
    'AZURE_AUTH_MODE',
    'AZURE_TENANT_ID',
    'AZURE_CLIENT_ID',
    'AZURE_CLIENT_SECRET',
    'AZURE_TOKEN_SCOPE',
)

# Keys owned by the GitHub tool import prompts, cleared and rebuilt the same
# way as PROVIDER_ENV_KEYS.
GITHUB_ENV_KEYS = (
    'GITHUB_TOKEN',
    'GITHUB_BASE_URL',
)


def configure_tool_server(existing_env=None) -> dict:
    env = existing_env or {}
    existing_api_key = env.get('API_KEY', None)
    if not existing_api_key:
        env['API_KEY'] = generate_random_password(40)
    return env


def _prompt_with_default(label, current, default=None, is_secret=False):
    """Show current value (masked if secret), accept ENTER to keep, or new input."""
    prompt_fn = masked_input if is_secret else input
    if current:
        display = f"{'*' * len(current[:-4])}{current[-4:]}" if is_secret else current
        modify = input(f'\n{label} is set to "{display}".\nPress ENTER to keep, or "1" to modify: ')
        if modify == '':
            return current
    hint = f' [{default}]' if default else ''
    value = prompt_fn(f'\nEnter {label}{hint}: ')
    if not value and default is not None:
        value = default
    return value


def _ensure_provider_prefix(model, provider):
    """Force a ``provider:model`` string."""
    if not model:
        return model
    return f'{provider}:{model}'


# Three model tiers used across the codebase. The labels below get appended to
# each prompt so the operator can reason about which deployment to map.
TIER_PROMPTS = (
    ('LLM_MODEL', 'reasoning and planning'),
    ('LLM_MODEL_MD', 'plan execution'),
    ('LLM_MODEL_SM', 'classification and routing'),
)


def _prompt_model_tiers(mara_env, defaults, label_prefix='', provider_prefix=None):
    """Prompt for LLM/MD/SM model deployments with per-tier defaults.

    ``defaults`` is a 3-tuple aligned with ``TIER_PROMPTS``. ``label_prefix``
    lets the Azure flow tag prompts as deployment names. ``provider_prefix``
    forces ``provider:model`` strings (used for Anthropic).
    """
    out = {}
    prefix_token = f'{provider_prefix}:' if provider_prefix else None
    for (key, purpose), default in zip(TIER_PROMPTS, defaults):
        label = f'{key}{label_prefix} ({purpose})'
        existing = mara_env.get(key)
        if prefix_token and existing and existing.startswith(prefix_token):
            existing = existing[len(prefix_token):]
        value = _prompt_with_default(label, existing, default=default)
        if provider_prefix:
            value = _ensure_provider_prefix(value, provider_prefix)
        out[key] = value
    return out


def configure_openai_envvars(mara_env) -> dict:
    """Plain OpenAI (or any OpenAI-compatible endpoint, e.g. self-hosted vLLM)."""
    llm_api_url = _prompt_with_default(
        'LLM_API_URL',
        mara_env.get('LLM_API_URL'),
        default='https://api.openai.com/v1',
    )
    tiers = _prompt_model_tiers(
        mara_env,
        defaults=('gpt-5.5', 'gpt-5.4-mini', 'gpt-5.4-mini'),
    )
    return {'LLM_API_URL': llm_api_url, **tiers}


def configure_anthropic_envvars(mara_env) -> dict:
    """Anthropic API. Uses LLM_API_KEY for the Anthropic key.

    Models are stored as ``anthropic:<name>`` so the provider-aware client
    factory routes correctly even if other settings change later.

    Anthropic has no embeddings API, so the operator picks a separate
    embedding provider — Voyage AI (managed) or a local HuggingFace TEI
    container served on an OpenAI-compatible endpoint.
    """
    env: dict = _prompt_model_tiers(
        mara_env,
        defaults=(
            'claude-opus-4-7',
            'claude-sonnet-4-6',
            'claude-haiku-4-5',
        ),
        provider_prefix='anthropic',
    )

    existing_embedding = mara_env.get('EMBEDDING_MODEL') or ''
    default_choice = 2 if existing_embedding and not existing_embedding.startswith('voyage:') else 1
    embedding_choice = ask_selection(
        'Anthropic has no embeddings API. Which embedding provider should MARA use?',
        [
            'Voyage AI (managed, requires API key)',
            'Local (HuggingFace TEI container — slow on CPU, uses ~2GB RAM, '
            'and first startup can take a few minutes while the model downloads)',
        ],
        default=default_choice,
    )

    if embedding_choice == '1':
        existing_voyage_model = existing_embedding
        if existing_voyage_model.startswith('voyage:'):
            existing_voyage_model = existing_voyage_model[len('voyage:'):]
        else:
            existing_voyage_model = ''
        voyage_model = _prompt_with_default(
            'EMBEDDING_MODEL (Voyage model)',
            existing_voyage_model,
            default='voyage-4-large',
        )
        env['EMBEDDING_MODEL'] = _ensure_provider_prefix(voyage_model, 'voyage')
        env['VOYAGE_API_KEY'] = _prompt_with_default(
            'VOYAGE_API_KEY',
            mara_env.get('VOYAGE_API_KEY'),
            is_secret=True,
        )
    else:
        # Local TEI runs as the `mara-hf-tei` service in docker-compose-mara.yaml,
        # gated by the `local-embeddings` compose profile. setup.py turns
        # the profile on by writing COMPOSE_PROFILES into the root .env.
        local_default_model = 'BAAI/bge-large-en-v1.5'
        existing_local_model = existing_embedding
        if existing_local_model.startswith(('voyage:', 'bedrock:')):
            existing_local_model = ''
        env['EMBEDDING_MODEL'] = _prompt_with_default(
            'EMBEDDING_MODEL (HuggingFace model id served by TEI)',
            existing_local_model,
            default=local_default_model,
        )
        # Joins the compose network — no need to publish the TEI port to the host.
        env['EMBEDDING_API_URL'] = 'http://mara-hf-tei:80/v1'
        # TEI ignores the auth header but the OpenAI client requires a non-empty key.
        env['EMBEDDING_API_KEY'] = 'unused'

    return env


def configure_bedrock_envvars(mara_env) -> dict:
    """AWS Bedrock. Reuses the Anthropic Messages API surface through the
    ``anthropic[bedrock]`` SDK, so only the model IDs and AWS auth differ.

    Models are stored as ``bedrock:<bedrock-model-id>``..
    """
    env: dict = {}

    print("\nNote: chat models must be Anthropic Claude on Bedrock.")
    print("Embedding models support Cohere (cohere.*) and Amazon Titan (amazon.titan-embed-*).")

    region = _prompt_with_default(
        'AWS_REGION',
        mara_env.get('AWS_REGION'),
        default='us-east-1',
    )
    env['AWS_REGION'] = region

    tiers = _prompt_model_tiers(
        mara_env,
        defaults=(
            'global.anthropic.claude-opus-4-7',
            'global.anthropic.claude-sonnet-4-6',
            'global.anthropic.claude-haiku-4-5-20251001-v1:0',
        ),
        provider_prefix='bedrock',
    )
    env.update(tiers)

    existing_embedding = mara_env.get('EMBEDDING_MODEL') or ''
    if existing_embedding.startswith('bedrock:'):
        existing_embedding = existing_embedding[len('bedrock:'):]
    embedding_model = _prompt_with_default(
        'EMBEDDING_MODEL (Bedrock model ID)',
        existing_embedding,
        default='cohere.embed-v4:0',
    )
    env['EMBEDDING_MODEL'] = _ensure_provider_prefix(embedding_model, 'bedrock')

    # Auth: access keys only
    env['AWS_AUTH_MODE'] = 'access_key'
    env['AWS_ACCESS_KEY_ID'] = _prompt_with_default(
        'AWS_ACCESS_KEY_ID', mara_env.get('AWS_ACCESS_KEY_ID'),
    )
    env['AWS_SECRET_ACCESS_KEY'] = _prompt_with_default(
        'AWS_SECRET_ACCESS_KEY',
        mara_env.get('AWS_SECRET_ACCESS_KEY'),
        is_secret=True,
    )
    session_token = _prompt_with_default(
        'AWS_SESSION_TOKEN (optional, leave blank if not using temporary creds)',
        mara_env.get('AWS_SESSION_TOKEN'),
        is_secret=True,
    )
    if session_token:
        env['AWS_SESSION_TOKEN'] = session_token

    return env


def configure_azure_envvars(mara_env) -> dict:
    """AOAI directly, or AOAI fronted by Azure API Management.

    Returns the deployment-name + version env, the chosen auth mode, and any
    APIM-related headers. The caller is responsible for collecting LLM_API_KEY
    when ``AZURE_AUTH_MODE`` ends up as ``api_key``.
    """
    llm_api_url = _prompt_with_default(
        'LLM_API_URL',
        mara_env.get('LLM_API_URL'),
    ) or input("\nEnter your Azure LLM API URL\n(e.g. https://example.azure.com or https://<apim>.azure-api.net): ")

    embedding_deployment = _prompt_with_default(
        'EMBEDDING_MODEL (Azure deployment name)',
        mara_env.get('EMBEDDING_MODEL'),
        default='text-embedding-ada-002',
    )
    tiers = _prompt_model_tiers(
        mara_env,
        defaults=('gpt-5.4', 'gpt-5.4-mini', 'gpt-5.4-mini'),
        label_prefix=' (Azure deployment name)',
    )
    azure_llm_api_version = _prompt_with_default(
        'AZURE_LLM_API_VERSION',
        mara_env.get('AZURE_LLM_API_VERSION'),
        default='2024-08-01-preview',
    )
    azure_embedding_api_version = _prompt_with_default(
        'AZURE_EMBEDDING_API_VERSION',
        mara_env.get('AZURE_EMBEDDING_API_VERSION'),
        default='2023-05-15',
    )

    env = {
        'OPENAI_API_TYPE': 'azure',
        'LLM_API_URL': llm_api_url,
        'AZURE_EMBEDDING_API_VERSION': azure_embedding_api_version,
        'AZURE_LLM_API_VERSION': azure_llm_api_version,
        'EMBEDDING_MODEL': embedding_deployment,
        **tiers,
    }

    # Auth method
    existing_mode = mara_env.get('AZURE_AUTH_MODE', 'api_key').lower()
    default_choice = {'api_key': 1, 'service_principal': 2}.get(existing_mode, 1)
    auth_choice = ask_selection(
        'How should the container authenticate to Azure?',
        [
            'API key (LLM_API_KEY)',
            'Service Principal (tenant + client id + client secret)',
        ],
        default=default_choice,
    )

    if auth_choice == '1':
        env['AZURE_AUTH_MODE'] = 'api_key'
    else:
        env['AZURE_AUTH_MODE'] = 'service_principal'
        env['AZURE_TENANT_ID'] = _prompt_with_default(
            'AZURE_TENANT_ID', mara_env.get('AZURE_TENANT_ID'),
        )
        env['AZURE_CLIENT_ID'] = _prompt_with_default(
            'AZURE_CLIENT_ID', mara_env.get('AZURE_CLIENT_ID'),
        )
        env['AZURE_CLIENT_SECRET'] = _prompt_with_default(
            'AZURE_CLIENT_SECRET',
            mara_env.get('AZURE_CLIENT_SECRET'),
            is_secret=True,
        )
        env['AZURE_TOKEN_SCOPE'] = _prompt_with_default(
            'AZURE_TOKEN_SCOPE',
            mara_env.get('AZURE_TOKEN_SCOPE'),
            default='https://cognitiveservices.azure.com/.default',
        )

    # APIM subscription key (optional, layered on top of the auth above)
    use_apim_key = ask_selection(
        'Is your endpoint fronted by APIM with a subscription key?',
        ['No', 'Yes'],
        default=2 if mara_env.get('LLM_API_HEADERS') else 1,
    )
    if use_apim_key == '2':
        existing_headers = mara_env.get('LLM_API_HEADERS') or ''
        existing_sub_key = ''
        try:
            parsed = json.loads(existing_headers) if existing_headers else {}
            existing_sub_key = parsed.get('Ocp-Apim-Subscription-Key', '')
        except json.JSONDecodeError:
            pass
        sub_key = _prompt_with_default(
            'APIM subscription key (Ocp-Apim-Subscription-Key)',
            existing_sub_key,
            is_secret=True,
        )
        if sub_key:
            env['LLM_API_HEADERS'] = json.dumps({'Ocp-Apim-Subscription-Key': sub_key})

    return env


def _collect_llm_api_key(existing_mara_env) -> str:
    return _prompt_with_default(
        'LLM_API_KEY',
        existing_mara_env.get('LLM_API_KEY'),
        is_secret=True,
    )


def configure_github_envvars(mara_env) -> dict:
    """Optional settings for importing custom tools from GitHub repos.

    The feature works with no configuration: public repos are analyzed
    unauthenticated and users connect personal tokens for private repos.
    A server-wide token raises the public-repo API rate limit from 60 to
    5000 req/hr; the base URL only changes for GitHub Enterprise Server.
    """
    existing_token = mara_env.get('GITHUB_TOKEN') or ''
    existing_base = mara_env.get('GITHUB_BASE_URL') or ''
    already_configured = bool(existing_token or existing_base)

    choice = ask_selection(
        'MARA can import custom tools from GitHub repositories. This works out '
        'of the box;\noptional settings raise API rate limits and add GitHub '
        'Enterprise Server support.\nConfigure them?',
        ['No (defaults: github.com, no server token)', 'Yes'],
        default=2 if already_configured else 1,
    )
    if choice == '1':
        return {}

    env = {}
    base_url = _prompt_with_default(
        'GITHUB_BASE_URL (GitHub Enterprise Server: https://<host>/api/v3)',
        existing_base,
        default='https://api.github.com',
    )
    if base_url and base_url != 'https://api.github.com':
        env['GITHUB_BASE_URL'] = base_url

    token = _prompt_with_default(
        'GITHUB_TOKEN (optional, ENTER to skip — server-wide token for '
        'public-repo analysis; a no-scope PAT is enough)',
        existing_token,
        is_secret=True,
    )
    if token:
        env['GITHUB_TOKEN'] = token

    return env


def configure_mara_server(existing_mara_env) -> dict:
    # Strip every provider-related key from the existing env. The function
    # rebuilds them based on the user's current selections; leaving stale keys
    # in (e.g. LLM_API_KEY when switching to service_principal, or AZURE_* when
    # switching to Anthropic) silently breaks runtime config.
    base_env = {
        k: v for k, v in existing_mara_env.items()
        if k not in PROVIDER_ENV_KEYS + GITHUB_ENV_KEYS
    }
    env = dict(base_env)

    # Detect prior provider for the default selection
    prior_type = (existing_mara_env.get('OPENAI_API_TYPE') or '').lower()
    prior_model = existing_mara_env.get('LLM_MODEL') or ''
    if prior_type == 'azure':
        prior_provider = 4
    elif prior_model.startswith('bedrock:'):
        prior_provider = 3
    elif prior_model.startswith('anthropic:'):
        prior_provider = 1
    else:
        prior_provider = 2

    existing_llm_key = existing_mara_env.get('LLM_API_KEY', None)
    enable_ai = ask_selection(
        'Would you like to set up AI chat features?',
        ['Yes', 'No (workspace management only)'],
        default=2 if existing_llm_key == '' else 1,
    )
    if enable_ai == '2':
        env['LLM_API_KEY'] = ''
        # GitHub tool import is independent of AI chat features
        env.update(configure_github_envvars(existing_mara_env))
        return env

    llm_provider = ask_selection(
        'Which LLM provider are you using?',
        ['Anthropic', 'OpenAI', 'AWS Bedrock', 'Microsoft Foundry (Azure)'],
        default=prior_provider,
    )

    same_provider = llm_provider == str(prior_provider)
    config_env = existing_mara_env if same_provider else base_env

    if llm_provider == '1':
        env.update(configure_anthropic_envvars(config_env))
        env['LLM_API_KEY'] = _collect_llm_api_key(config_env)
    elif llm_provider == '2':
        env.update(configure_openai_envvars(config_env))
        env['LLM_API_KEY'] = _collect_llm_api_key(config_env)
    elif llm_provider == '3':
        # Bedrock authenticates via AWS creds, not LLM_API_KEY.
        env.update(configure_bedrock_envvars(config_env))
    elif llm_provider == '4':
        provider_env = configure_azure_envvars(config_env)
        env.update(provider_env)
        # Service principal / managed identity flows authenticate via Entra
        # tokens; no static API key is needed (or wanted) in that case.
        if provider_env.get('AZURE_AUTH_MODE') == 'api_key':
            env['LLM_API_KEY'] = _collect_llm_api_key(config_env)

    env.update(configure_github_envvars(existing_mara_env))
    return env
