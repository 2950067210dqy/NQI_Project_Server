from typing import Dict, Optional, List
from datetime import datetime

from fastapi import WebSocket
from loguru import logger


class WebSocketManager:
    """管理 WebSocket 连接并广播通知"""

    def __init__(self):
        # {websocket: {"client_type": "upper"/"device", "device_id": str | None}}
        self.active_connections: Dict[WebSocket, Dict[str, Optional[str]]] = {}
        
        # ✅ 通知暂存：当没有上位机连接时暂存通知
        self.pending_notifications: List[dict] = []
        self.max_pending_notifications = 100  # 最多暂存100条通知

    async def connect(self, websocket: WebSocket, client_type: str, device_id: Optional[str] = None):
        """接受连接并记录客户端信息"""
        await websocket.accept()
        self.active_connections[websocket] = {
            "client_type": client_type,
            "device_id": device_id
        }
        logger.info(f"WebSocket connected: {client_type} (device_filter={device_id})")
        
        # ✅ 如果是上位机连接，发送所有暂存的通知
        if client_type == "upper":
            await self.send_pending_notifications(websocket, device_id)

    def disconnect(self, websocket: WebSocket):
        """移除连接"""
        if websocket in self.active_connections:
            meta = self.active_connections.pop(websocket)
            logger.info(
                f"WebSocket disconnected: {meta.get('client_type')} (device_filter={meta.get('device_id')})"
            )

    async def broadcast_notification(self, payload: dict):
        """
        广播通知到所有上位机连接。
        如果连接带 device_id 过滤，则只推送对应设备的数据。
        """
        # ✅ 检查是否有上位机连接
        has_upper_client = any(
            meta.get("client_type") == "upper" 
            for meta in self.active_connections.values()
        )
        
        # ✅ 如果没有上位机连接，暂存通知
        if not has_upper_client:
            self.pending_notifications.append(payload)
            # 限制暂存数量
            if len(self.pending_notifications) > self.max_pending_notifications:
                self.pending_notifications.pop(0)  # 移除最旧的通知
            logger.info(f"No upper client connected, notification saved. Pending: {len(self.pending_notifications)}")
            return
        
        stale = []
        for websocket, meta in list(self.active_connections.items()):
            if meta.get("client_type") != "upper":
                continue

            device_filter = meta.get("device_id")
            if device_filter and payload.get("device_id") != device_filter:
                continue

            try:
                await websocket.send_json(payload)
            except Exception as e:
                logger.warning(f"WebSocket send failed, will drop connection: {e}")
                stale.append(websocket)

        for ws in stale:
            self.disconnect(ws)
    
    async def send_pending_notifications(self, websocket: WebSocket, device_filter: Optional[str] = None):
        """
        发送暂存的通知给新连接的上位机
        """
        if not self.pending_notifications:
            return
        
        sent_count = 0
        for notification in self.pending_notifications:
            # 如果有设备过滤，只发送匹配的通知
            if device_filter and notification.get("device_id") != device_filter:
                continue
            
            try:
                await websocket.send_json(notification)
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send pending notification: {e}")
                break
        
        logger.info(f"Sent {sent_count} pending notifications to new upper client")
        
        # 清空已发送的通知
        self.pending_notifications.clear()


# 全局单例，供 main.py 引用
ws_manager = WebSocketManager()

