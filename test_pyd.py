from pydantic import BaseModel
class M(BaseModel):
    x: bool
print(M(x="'False'").x)
