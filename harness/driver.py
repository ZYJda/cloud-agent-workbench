import os
import json
from dataclasses import dataclass
from pathlib import Path
from .models import configure_model

@dataclass(frozen=True)
class LaunchSpec:
    args: list[str]
    env: dict[str,str]
    cwd: Path
    prompt: str

class CodexDriver:
    def prepare(self, ctx, thread_id=None):
        if not os.getenv('AGENT_API_KEY') or not os.getenv('AGENT_MODEL'):
            raise RuntimeError('请在服务端 .env 配置 AGENT_API_KEY 和 AGENT_MODEL 后重启。')
        home = ctx.cli_home
        home.mkdir(parents=True, exist_ok=True)
        config = '\n'.join(configure_model(home, os.environ['AGENT_MODEL'])+[
            'model_provider = "exam"',
            'model = '+json.dumps(os.environ['AGENT_MODEL']),
            'web_search = "disabled"',
            '[model_providers.exam]', 'name = "Configured API"',
            'base_url = '+json.dumps(os.getenv('AGENT_BASE_URL','https://api.openai.com/v1')),
            'env_key = "AGENT_API_KEY"', 'wire_api = "responses"',
            'request_max_retries = 2', 'stream_max_retries = 2',
            '[agents]', 'enabled = false',
        ])
        (home/'config.toml').write_text(config, encoding='utf-8')
        env = {k:v for k,v in os.environ.items() if k.upper() in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','HOME','USERPROFILE','COMSPEC','PATHEXT','LANG')}
        env.update(CODEX_HOME=str(home), AGENT_API_KEY=os.environ['AGENT_API_KEY'], NO_COLOR='1')
        args = [os.getenv('CODEX_BIN','codex'), 'exec']
        if thread_id:
            args += ['resume', thread_id]
        args += ['--json','--skip-git-repo-check','--dangerously-bypass-approvals-and-sandbox','-']
        return LaunchSpec(args,env,ctx.workspace,ctx.prompt)
