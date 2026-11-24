"""CapabilityRegistry and ScopedRoleToolExecutor for Prompt 05 Adaptive Incident Taskforce.

Hard Domain Invariants:
1. Logical Roles Only (Finance, Operations, Risk, Support): Each role has an explicit,
   immutable read-tool allowlist (`allowed_read_tools`).
2. Strict Read-Only Enforcement: `can_execute_writes` is ALWAYS False for every role.
   No role can directly mutate SimOrder, SimPayment, SimRefund, or Case state.
3. Role Scope Boundary (QA I04): Attempting to invoke any tool outside the role's
   `allowed_read_tools` raises 403 FORBIDDEN (`ROLE_SCOPE_VIOLATION`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.connectors.read_tools import ReadToolRegistry, ToolResult
from app.core.errors import AppError, ErrorCode
from app.models.enums import TaskforceRole


@dataclass(frozen=True)
class RoleCapabilitySpec:
    role: str
    title_vi: str
    responsibility_vi: str
    allowed_read_tools: frozenset[str]
    can_propose_actions: bool = False
    can_execute_writes: bool = False


ROLE_CAPABILITIES: dict[str, RoleCapabilitySpec] = {
    TaskforceRole.FINANCE.value: RoleCapabilitySpec(
        role=TaskforceRole.FINANCE.value,
        title_vi="Chuyên viên Đối soát Tài chính (Finance)",
        responsibility_vi=(
            "Đối soát sổ cái thanh toán cổng MoMo, xác minh trạng thái giao dịch (SUCCESS/FAILED) "
            "và lịch sử hoàn tiền."
        ),
        allowed_read_tools=frozenset(
            {
                "find_payment_candidates",
                "get_payment_record",
                "get_refund_history",
            }
        ),
        can_propose_actions=False,
        can_execute_writes=False,
    ),
    TaskforceRole.OPERATIONS.value: RoleCapabilitySpec(
        role=TaskforceRole.OPERATIONS.value,
        title_vi="Chuyên viên Vận hành Đơn hàng & Đồng bộ (Operations)",
        responsibility_vi=(
            "Kiểm tra trạng thái đơn hàng, nhật ký sự kiện worker/batch đồng bộ webhook "
            "và tồn kho vận hành."
        ),
        allowed_read_tools=frozenset(
            {
                "get_order",
                "get_order_events",
                "get_inventory",
            }
        ),
        can_propose_actions=False,
        can_execute_writes=False,
    ),
    TaskforceRole.RISK.value: RoleCapabilitySpec(
        role=TaskforceRole.RISK.value,
        title_vi="Chuyên viên Kiểm soát Rủi ro & Chính sách (Risk)",
        responsibility_vi=(
            "Kiểm tra trùng lặp hoàn tiền, hạn mức số dư khả dụng và quy định chính sách "
            "trước khi cho phép phê duyệt."
        ),
        allowed_read_tools=frozenset(
            {
                "get_refund_history",
                "get_payment_record",
                "get_policy",
            }
        ),
        can_propose_actions=False,
        can_execute_writes=False,
    ),
    TaskforceRole.SUPPORT.value: RoleCapabilitySpec(
        role=TaskforceRole.SUPPORT.value,
        title_vi="Chuyên viên Hỗ trợ Khách hàng (Support)",
        responsibility_vi=(
            "Tra cứu thông tin đơn hàng cơ bản và chính sách hỗ trợ để phản hồi khách hàng."
        ),
        allowed_read_tools=frozenset(
            {
                "get_order",
                "get_policy",
            }
        ),
        can_propose_actions=False,
        can_execute_writes=False,
    ),
}


class CapabilityRegistry:
    """Registry of bounded logical taskforce roles and their strict read-tool permissions."""

    @staticmethod
    def get_spec(role: str) -> RoleCapabilitySpec:
        norm = role.strip().upper()
        spec = ROLE_CAPABILITIES.get(norm)
        if spec is None:
            raise AppError(
                ErrorCode.BAD_REQUEST,
                f"Unknown taskforce role '{role}'. Allowed roles: {sorted(ROLE_CAPABILITIES)}",
                status_code=400,
            )
        return spec

    @staticmethod
    def list_specs() -> list[RoleCapabilitySpec]:
        return [
            ROLE_CAPABILITIES[TaskforceRole.FINANCE.value],
            ROLE_CAPABILITIES[TaskforceRole.OPERATIONS.value],
            ROLE_CAPABILITIES[TaskforceRole.RISK.value],
            ROLE_CAPABILITIES[TaskforceRole.SUPPORT.value],
        ]

    @classmethod
    def assert_tool_allowed(cls, role: str, tool_name: str) -> RoleCapabilitySpec:
        spec = cls.get_spec(role)
        clean_tool = tool_name.strip()
        if clean_tool not in spec.allowed_read_tools:
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"ROLE_SCOPE_VIOLATION: Role '{spec.role}' is not authorized to invoke tool "
                f"'{clean_tool}'. Allowed tools for {spec.role}: "
                f"{sorted(spec.allowed_read_tools)}",
                status_code=403,
                details={
                    "role": spec.role,
                    "attempted_tool": clean_tool,
                    "allowed_read_tools": sorted(spec.allowed_read_tools),
                },
            )
        return spec


class ScopedRoleToolExecutor:
    """Enforces role-scoped read-only tool execution for a specific TaskforceRole (QA I04)."""

    def __init__(self, *, role: str, tool_registry: ReadToolRegistry) -> None:
        self._spec = CapabilityRegistry.get_spec(role)
        self._tool_registry = tool_registry
        self._invocations: list[dict[str, Any]] = []

    @property
    def role(self) -> str:
        return self._spec.role

    @property
    def allowed_tools(self) -> list[str]:
        return sorted(self._spec.allowed_read_tools)

    @property
    def invocations(self) -> list[dict[str, Any]]:
        return list(self._invocations)

    def execute_read_tool(self, tool_name: str, args: dict[str, Any]) -> ToolResult:
        """Validate role scope before delegating to ReadToolRegistry."""
        CapabilityRegistry.assert_tool_allowed(self._spec.role, tool_name)
        result = self._tool_registry.execute(tool_name, args)
        self._invocations.append(
            {
                "role": self._spec.role,
                "tool_name": tool_name,
                "args": dict(args),
                "status": result.status,
                "source": result.source,
                "source_ref": result.source_ref,
            }
        )
        return result
