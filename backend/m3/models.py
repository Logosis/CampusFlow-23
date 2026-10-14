from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

# 本模块自己的ORM模型写在此处或从此处导入；禁止导入其他模块的业务模型。
