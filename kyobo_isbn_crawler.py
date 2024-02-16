import sys
from datetime import datetime, date
import dotenv
import os
import requests
import boto3
from elasticsearch import Elasticsearch
from elasticsearch import helpers

dotenv.load_dotenv()


DATE_FORMAT = os.getenv('DATE_FORMAT')
ELASTIC_PASSWORD = os.getenv('ELASTIC_PASSWORD')
CLOUD_ID = os.getenv('CLOUD_ID')
ELASTIC_ID = os.getenv('ELASTIC_ID')
ELASTIC_INDEX_NAME = os.getenv('ELASTIC_INDEX_NAME')
REGION_NAME = os.getenv('REGION_NAME')
ACCESS_KEY_ID = os.getenv('ACCESS_KEY_ID')
SECRET_ACCESS_KEY= os.getenv('SECRET_ACCESS_KEY')
DYNAMODB_TABLE_NAME = os.getenv('DYNAMODB_TABLE_NAME')
DYNAMODB_INDEX_NAME = os.getenv('DYNAMODB_INDEX_NAME')
DYNAMODB_SORT_BY_PROPERTY = os.getenv('DYNAMODB_SORT_BY_PROPERTY')


if(DATE_FORMAT is None or
    ELASTIC_PASSWORD is None or
    CLOUD_ID is None or
    ELASTIC_ID is None or
    ELASTIC_INDEX_NAME is None or
    REGION_NAME is None or
    ACCESS_KEY_ID is None or
    SECRET_ACCESS_KEY is None or
    DYNAMODB_TABLE_NAME is None or
    DYNAMODB_INDEX_NAME is None or
    DYNAMODB_SORT_BY_PROPERTY is None):
    raise RuntimeError("환경 변수 정보를 가져오지 못했습니다.")




def isBeforeOrSame(a, b):
    global DATE_FORMAT

    if not isinstance(a, date) :
        a = datetime.strptime(str(a), DATE_FORMAT).date()
    
    if not isinstance(b,date) : 
        b= datetime.strptime(str(b), DATE_FORMAT).date()
    
    if a <= b : #a 날짜가 b 날짜보다 같거나 이전일때
        return True
    else: #a 날짜가 b날짜보다 이후일때
        return False


def findMaxCrawlingTimeFromDynamoDB(dynamodb_client, table_name, index_name, sort_by_property):
    try : 
        response = dynamodb_client.query(
        TableName=table_name,
        IndexName = index_name,
        KeyConditionExpression = 'static_number = :staticnum',
        ExpressionAttributeValues= {
            ':staticnum': {'N': '1'}  # GSI's partition key value
        },
        Limit=1,  # limit the result to 1 to get only one row
        ProjectionExpression = sort_by_property,  # Limit Select clause to Id column/property only
        ScanIndexForward=False # Scan the results in descending order to get maximum ID rows in the first row
        )
        return int(response['Items'][0][sort_by_property]['N']) #필드중 가장 큰값 
    except:
        raise RuntimeError("다이나모 DB에서 가장 큰값을 가져오는데 문제가 발생했습니다.")



def pushBulkDataElasticIndex(client,index_name, data_list) :
    actions = [
    {
        "_index": index_name,
        "_id": document['isbn'], #document id를 isbn로 두기
        "_source": document
    }
    for document in data_list
    ]

    response = helpers.bulk(client, actions)

    if response[0] == len(actions):
        return 0
    else:
        raise RuntimeError("데이터가 온전하게 전송되지 못하였습니다.")


def pushRecentPubDynamoDB(table, today, payload):
    try: 
        global DATE_FORMAT
        if isinstance(today,date):
            date_number = int(today.strftime(DATE_FORMAT))
        else :
            dateToday = datetime.strptime(str(today), DATE_FORMAT).date()
            date_number = int(dateToday.strftime(DATE_FORMAT))
    except:
        raise RuntimeError("올바르지 않은 날짜 형식입니다.")
    
    try :
        table.put_item(
        Item={
            "crawling_time" : date_number,
            "payload": payload,
            "static_number" : 1 
        }
    )
    except:
        raise RuntimeError("다이나모 db 통신에 실패했습니다.")
    



#0. 엘라스틱 서치 연결

client = Elasticsearch(
    cloud_id=CLOUD_ID,
    basic_auth=(ELASTIC_ID, ELASTIC_PASSWORD)
)

client.info()


#0. dynamo db 연결
dynamodb_client = boto3.client('dynamodb', region_name=REGION_NAME, aws_access_key_id=ACCESS_KEY_ID, aws_secret_access_key=SECRET_ACCESS_KEY)

dynamodb = boto3.resource('dynamodb', region_name=REGION_NAME, aws_access_key_id=ACCESS_KEY_ID, aws_secret_access_key=SECRET_ACCESS_KEY)

table = dynamodb.Table(DYNAMODB_TABLE_NAME)







#1. dynamo db에서 직전 크롤링 시간의 각 카테고리별 코드와 최신 출간일 가져오기
max_crawling_time_val = findMaxCrawlingTimeFromDynamoDB(dynamodb_client, DYNAMODB_TABLE_NAME, DYNAMODB_INDEX_NAME, DYNAMODB_SORT_BY_PROPERTY)
print("직전 크롤링 시간 : ",max_crawling_time_val)

#2. 직전 크롤링시간이 현재보다 앞서면 크롤링 중지
if(isBeforeOrSame(date.today(), max_crawling_time_val)):
    sys.exit(0)


#3. 직전 크롤링시간에 저장해놓은 각 카테고리별 최신 출간일 가져오기
response2 = table.get_item(Key={DYNAMODB_SORT_BY_PROPERTY: max_crawling_time_val})
cate_code_recent_pub_date_list = response2['Item']['payload']


docu_list=[]
docu = {}

payload=[]
payload_data={}


headers = {
    'user-agent' : 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
}


for i in range(len(cate_code_recent_pub_date_list)):
    cate = cate_code_recent_pub_date_list[i]['cate']
    code = cate_code_recent_pub_date_list[i]['code']
    pub = cate_code_recent_pub_date_list[i]['recent_pub_date']

    flag = False

    for j in range(20):
        if(flag) :
            if(len(docu_list)!=0):
                #모아놓은 isbn push
                pushBulkDataElasticIndex(client, ELASTIC_INDEX_NAME, docu_list)
                docu_list = [] #리스트 초기화
            break

        url = f'https://product.kyobobook.co.kr/api/gw/pdt/category/all?page={j+1}&per=20&saleCmdtDvsnCode=KOR&saleCmdtClstCode={code}&sort=new'
        response = requests.get(url, headers=headers)
        data_json = response.json()

        for i in range(100):
            try :
                now_pub = data_json['data']['tabContents'][i]['rlseDate']
                isbn = data_json['data']['tabContents'][i]['cmdtcode']

                #4. 각 카테고리별 최신출간일 다이나모 db에 저장
                if(j==0 and i==0):
                    payload_data['code'] = code
                    payload_data['cate'] = cate
                    payload_data['recent_pub_date'] = now_pub
                    
                    payload.append(payload_data)
                    payload_data={}
                

                #5. 각 카테고리별 최신 출간일이 1에서의 최신출간일보다 늦으면 다음 카테고리로
                if(not isBeforeOrSame(pub, now_pub)): 
                    flag=True
                    break

                docu['category'] = cate
                docu['isbn'] = isbn
                docu['crawling_date'] = date.today().isoformat()
                docu_list.append(docu)

                print(docu)
                docu={} # 딕셔너리 초기화

                if len(docu_list) >= 300:
                    #모아놓은 isbn push
                    pushBulkDataElasticIndex(client, ELASTIC_INDEX_NAME, docu_list)
                    docu_list = [] #리스트 초기화
       
            except IndexError:
                break


if(len(docu_list)!=0) :
    #6. 모아놓은 isbn push
    pushBulkDataElasticIndex(client, ELASTIC_INDEX_NAME, docu_list)

if(len(payload)!=0) :
    #7. 모아놓은 크롤링한 최신 출간일 push
    pushRecentPubDynamoDB(table,date.today(),payload)



    