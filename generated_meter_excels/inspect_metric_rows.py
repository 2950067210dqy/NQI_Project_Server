from pathlib import Path
import sys
ROOT = Path(r"D:\WorkSpace\NQI_Project_Server")
sys.path.insert(0, str(ROOT))
from app.data_processing import parse_excel_file
sample = Path(r"D:\BaiduSyncdisk\NQI\数据集\电量数据 - 数值\TA3310三相表数据.xlsx")
payload = parse_excel_file(sample)
for sheet, sheet_payload in payload['parsed_data'].items():
    print('sheet', sheet)
    for metric in sheet_payload['data']:
        rows = [m['source_excel_row'] for m in metric['data']['x']['point_meta']]
        print(metric['name'], len(rows), rows[:5], rows[-5:], min(rows), max(rows))
