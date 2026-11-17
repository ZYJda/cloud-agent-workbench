import os
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Settings:
    data_dir: Path
    workers: int
    timeout: int

    @classmethod
    def from_env(cls, root):
        return cls(Path(os.getenv('DATA_DIR',root/'data')).resolve(),
                   max(1,min(4,int(os.getenv('MAX_CONCURRENT_RUNS','2')))),
                   int(os.getenv('RUN_TIMEOUT_SECONDS','900')))
