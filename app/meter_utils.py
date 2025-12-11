"""
三相表数据处理工具
"""
from pathlib import Path
from typing import Dict, Optional, Tuple
import openpyxl
from loguru import logger


class MeterExcelParser:
    """三相表Excel数据解析器"""

    @staticmethod
    def parse_excel(file_path: Path) -> Dict:
        """
        解析三相表Excel文件
        提取关键的电量数据参数
        """
        try:
            wb = openpyxl.load_workbook(file_path)
            ws = wb.active

            data = {
                'measurement_date': None,
                'meter_reading': None,
                'total_energy': None,
                'a_phase_voltage': None,
                'b_phase_voltage': None,
                'c_phase_voltage': None,
                'a_phase_current': None,
                'b_phase_current': None,
                'c_phase_current': None,
                'power_factor': None
            }

            # 遍历工作表寻找关键数据
            for row in ws.iter_rows(values_only=True):
                if row[0] is None:
                    continue

                cell_value = str(row[0]).strip()

                # 查找各项参数
                if '测量日期' in cell_value and len(row) > 1:
                    data['measurement_date'] = row[1]
                elif '电表读数' in cell_value and len(row) > 1:
                    try:
                        data['meter_reading'] = float(row[1])
                    except:
                        pass
                elif '总电能' in cell_value and len(row) > 1:
                    try:
                        data['total_energy'] = float(row[1])
                    except:
                        pass
                elif 'A相电压' in cell_value and len(row) > 1:
                    try:
                        data['a_phase_voltage'] = float(row[1])
                    except:
                        pass
                elif 'B相电压' in cell_value and len(row) > 1:
                    try:
                        data['b_phase_voltage'] = float(row[1])
                    except:
                        pass
                elif 'C相电压' in cell_value and len(row) > 1:
                    try:
                        data['c_phase_voltage'] = float(row[1])
                    except:
                        pass
                elif 'A相电流' in cell_value and len(row) > 1:
                    try:
                        data['a_phase_current'] = float(row[1])
                    except:
                        pass
                elif 'B相电流' in cell_value and len(row) > 1:
                    try:
                        data['b_phase_current'] = float(row[1])
                    except:
                        pass
                elif 'C相电流' in cell_value and len(row) > 1:
                    try:
                        data['c_phase_current'] = float(row[1])
                    except:
                        pass
                elif '功率因数' in cell_value and len(row) > 1:
                    try:
                        data['power_factor'] = float(row[1])
                    except:
                        pass

            wb.close()
            logger.info(f"Excel数据解析成功: {file_path}")
            return data

        except Exception as e:
            logger.error(f"Excel数据解析失败: {e}")
            return {}


class MeterImageClassifier:
    """三相表图片分类器"""

    @staticmethod
    def classify_image(file_name: str) -> str:
        """
        根据文件名分类图片类型
        """
        file_name_lower = file_name.lower()

        if '表盘' in file_name_lower or 'dial' in file_name_lower:
            return '表盘'
        elif '显示屏' in file_name_lower or 'display' in file_name_lower or 'screen' in file_name_lower:
            return '显示屏'
        elif '液晶' in file_name_lower or 'lcd' in file_name_lower:
            return '液晶显示'
        elif '指针' in file_name_lower or 'pointer' in file_name_lower or 'analog' in file_name_lower:
            return '指针式'
        else:
            return '其他'


meter_excel_parser = MeterExcelParser()
meter_image_classifier = MeterImageClassifier()