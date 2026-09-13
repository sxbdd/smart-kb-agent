"""Pydantic 模型：API 请求 / 响应。"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class DocumentUploadResponse(BaseModel):
    document_id: str = Field(description="文档 ID（UUID）")
    filename: str = Field(description="原始文件名")
    chunk_count: int = Field(description="切分后的片段数")
    status: str = Field(description="状态：success / failed")
    message: Optional[str] = Field(default=None, description="附加信息（失败原因等）")


class DocumentInfo(BaseModel):
    document_id: str = Field(description="文档 ID（UUID）")
    filename: str = Field(description="原始文件名")
    file_type: str = Field(description="文件类型：pdf / md / txt")
    file_size: int = Field(description="文件大小（字节）")
    chunk_count: int = Field(description="切分后的片段数")
    uploaded_at: str = Field(description="上传时间")


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50, description="用户名")
    password: str = Field(min_length=6, max_length=64, description="密码")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, description="用户名")
    password: str = Field(min_length=1, description="密码")


class AuthResponse(BaseModel):
    token: str = Field(description="访问令牌（JWT）")
    username: str = Field(description="用户名")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, description="用户问题")
    conversation_id: Optional[str] = Field(default=None, description="对话 ID，为空则新建对话")
    top_k: Optional[int] = Field(default=None, description="检索片段数，为空则使用默认值")


class Source(BaseModel):
    document_id: str = Field(description="来源文档 ID")
    document_name: str = Field(description="来源文档名")
    chunk_id: str = Field(description="片段 ID")
    content: str = Field(description="片段原文")
    score: float = Field(description="相似度分数（0~1）")


class AskResponse(BaseModel):
    answer: str = Field(description="回答内容")
    sources: List[Source] = Field(default_factory=list, description="引用来源列表")
    conversation_id: str = Field(description="对话 ID")
    mode: str = Field(default="", description="实际走的分支：chat / rag / agent")
    timestamp: datetime = Field(description="回答时间")


class ConversationInfo(BaseModel):
    conversation_id: str = Field(description="对话 ID")
    title: str = Field(description="对话标题")
    created_at: str = Field(description="创建时间")
    updated_at: str = Field(description="最后更新时间")
    message_count: int = Field(description="消息条数")


class ConversationHistory(BaseModel):
    conversation_id: str = Field(description="对话 ID")
    title: str = Field(description="对话标题")
    messages: List[dict] = Field(description="消息列表")


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80, description="新的对话标题")


class EvaluationRequest(BaseModel):
    test_set_path: Optional[str] = Field(default=None, description="测试集 JSON 路径")
    top_k: Optional[int] = Field(default=None, description="检索片段数")