import asyncio
import typing
from typing import AsyncIterable

from google_a2a.common.server.task_manager import InMemoryTaskManager
from google_a2a.common.types import (
    Artifact,
    JSONRPCResponse,
    Message,
    SendTaskRequest,
    SendTaskResponse,
    SendTaskStreamingRequest,
    SendTaskStreamingResponse,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from my_project.agent import create_ollama_agent, run_ollama

class MyAgentTaskManager(InMemoryTaskManager):
    def __init__(
        self,
        ollama_host: str,
        ollama_model: typing.Union[None, str]
    ):
        super().__init__()
        if ollama_model is not None:
            self.ollama_agent = create_ollama_agent(
                ollama_base_url=ollama_host,
                ollama_model=ollama_model
            )
        else:
            self.ollama_agent = None

    async def on_send_task(self, request: SendTaskRequest) -> SendTaskResponse:
        await self.upsert_task(request.params)

        task_id = request.params.id
        received_text = request.params.message.parts[0].text
        response_text = f"on_send_task received: {received_text}"
        if self.ollama_agent is not None:
            response_text = await run_ollama(ollama_agent=self.ollama_agent, prompt=received_text)

        task = await self._update_task(
            task_id=task_id,
            task_state=TaskState.COMPLETED,
            response_text=response_text
        )

        return SendTaskResponse(id=request.id, result=task)

    async def _stream_3_messages(self, request: SendTaskStreamingRequest):
        task_id = request.params.id
        received_text = request.params.message.parts[0].text

        text_messages = ["one", "two", "three"]
        for text in text_messages:
            parts = [
                {
                    "type": "text",
                    "text": f"{received_text}: {text}",
                }
            ]
            message = Message(role="agent", parts=parts)
            is_last = text == text_messages[-1]
            task_state = TaskState.COMPLETED if is_last else TaskState.WORKING
            task_status = TaskStatus(
                state=task_state,
                message=message
            )
            task_update_event = TaskStatusUpdateEvent(
                id=task_id,
                status=task_status,
                final=is_last,
            )
            await self.enqueue_events_for_sse(
                task_id,
                task_update_event
            )

        # Запрос на дополнительный ввод от клиента
        ask_message = Message(
            role="agent",
            parts=[{
                "type": "text",
                "text": "Would you like more messages? (Y/N)"
            }]
        )
        task_update_event = TaskStatusUpdateEvent(
            id=task_id,
            status=TaskStatus(
                state=TaskState.INPUT_REQUIRED,
                message=ask_message
            ),
            final=True,
        )
        await self.enqueue_events_for_sse(
            task_id,
            task_update_event
        )

    async def on_send_task_subscribe(
        self,
        request: SendTaskStreamingRequest
    ) -> AsyncIterable[SendTaskStreamingResponse] | JSONRPCResponse:
        await self.upsert_task(request.params)

        task_id = request.params.id
        received_text = request.params.message.parts[0].text
        sse_event_queue = await self.setup_sse_consumer(task_id=task_id)

        # Если клиент ответил "N", завершить задачу
        if received_text == "N":
            task_update_event = TaskStatusUpdateEvent(
                id=request.params.id,
                status=TaskStatus(
                    state=TaskState.COMPLETED,
                    message=Message(
                        role="agent",
                        parts=[{
                            "type": "text",
                            "text": "All done!"
                        }]
                    )
                ),
                final=True,
            )
            await self.enqueue_events_for_sse(
                request.params.id,
                task_update_event,
            )
        else:
            # Если это не новый запрос и ответ не "N", продолжить стриминг
            asyncio.create_task(self._stream_3_messages(request))

        return self.dequeue_events_for_sse(
            request_id=request.id,
            task_id=task_id,
            sse_event_queue=sse_event_queue,
        )

    async def _update_task(
        self,
        task_id: str,
        task_state: TaskState,
        response_text: str,
    ) -> Task:
        task = self.tasks[task_id]
        agent_response_parts = [
            {
                "type": "text",
                "text": response_text,
            }
        ]
        task.status = TaskStatus(
            state=task_state,
            message=Message(
                role="agent",
                parts=agent_response_parts,
            )
        )
        task.artifacts = [
            Artifact(
                parts=agent_response_parts,
            )
        ]
        return task
