from bpmn2neo import load_and_embed
from bpmn2neo.settings import Settings

s = Settings() 
res = load_and_embed(bpmn_path='./data/bpmn/Order Process for Pizza.bpmn',model_key='test1',settings=s)
