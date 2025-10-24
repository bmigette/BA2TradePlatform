from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertInstance, ExpertSetting
from sqlmodel import select

session = get_db()

# Check expert 12
expert = session.get(ExpertInstance, 12)
print(f'Expert 12 exists: {expert is not None}')
if expert:
    print(f'Expert 12: {expert.expert} - {expert.alias}')

# Check settings for expert 12
settings = session.exec(select(ExpertSetting).where(ExpertSetting.instance_id == 12)).all()
print(f'Settings count for expert 12: {len(settings)}')
if settings:
    print('First 10 settings:')
    for s in settings[:10]:
        print(f'  - {s.key}')
