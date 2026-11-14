from datetime import datetime, timezone
from app.db import get_db
from app.models import User, MealEntry
from sqlalchemy import select

db = next(get_db())
try:
    # 检查管理员的登录时间
    admin = db.scalar(select(User).where(User.username == 'admin'))
    if admin and admin.last_login_at:
        print(f"管理员登录时间: {admin.last_login_at}")
        print(f"  年份: {admin.last_login_at.year}")
        print(f"  ISO: {admin.last_login_at.isoformat()}")
    
    # 检查饮食记录的时间
    meals = list(db.scalars(select(MealEntry).limit(3)))
    if meals:
        print(f"\n前3条饮食记录:")
        for m in meals:
            print(f"  - {m.description[:20]}... 时间: {m.eaten_at}, 年份: {m.eaten_at.year}")
    
finally:
    db.close()
