# D：公共层接口 v1

本版固定服务编号、函数名、输入输出；D-F01 身份权限、D-F02 空间座位、D-F03 通知、D-F04 审计范围不变。依据：[任务书](../../../../Pj-任务书.pdf)第 1 页。

**状态：开发中。D-S01 登录、D-S02 当前身份、D-S03 退出与 D-S04 权限判断已提供实现和测试代码；其他公开接口尚未实现，完整模块验收与跨模块联调尚未完成。** 初版采用账号密码登录、邮件通知；这是本组起始方案，后续调整须同步调用方。只约定公开接口，不设计内部表、类或任务。

公共规则见[开发指导：公共规则 v1](../../../开发指导.md#3-公共规则-v1)。下表“输出”均指成功返回的 data；函数统一接收 input。

记号：str 字符串、int 整数、bool 布尔、time 带时区时间；[] 数组，? 可省，/null 可空。未标 ? 的输入必填。列表统一返回 {items:[下表单项],nextCursor:str/null}，单条直接返回单项。

## 1. 身份与权限

| 服务／函数／调用方 | 输入格式 | 输出格式 |
| --- | --- | --- |
| D-S01 登录 login(input)／前端 | {account:str,password:str,requestId:str} | {credential:str,expiresAt:time,identity:与S02输出一致} |
| D-S02 当前身份 get_identity(input)／前端、M1、M2、M3 | {}；身份来自请求凭据 | {userId:str,displayName:str,departmentId:str/null,accountStatus:ACTIVE/DISABLED,roleIds:[str],grants:[{permissionCode:str,scope:{type:GLOBAL/DEPARTMENT/SPACE,id:str/null}}]} |
| D-S03 退出 logout(input)／前端 | {requestId:str} | {loggedOut:bool} |
| D-S04 权限判断 check_permission(input)／M1、M2、M3 | {permissionCode:str,targetScope?:{type:GLOBAL/DEPARTMENT/SPACE,id:str/null}} | {subjectType:USER/SYSTEM,subjectId:str,allowed:bool,allowedScopes:[与targetScope同格式],reason:str} |

S02 仅 USER 模式查询当前用户。错误密码、禁用账号拒绝登录；原登录请求重试返回原有效凭据，原凭据已失效则拒绝，重新登录用新请求编号。重复退出或已失效凭据安全返回 loggedOut=true，只影响当前凭据。

D 实现细化：S02 的 roleIds 只返回当前 ACTIVE 的已分配角色；停用角色的关联仍保留供 S05 管理查询，但不进入当前有效身份。grants 仅汇总这些有效角色的授权，按“权限编码、范围类型、范围标识”去重，保留不同范围；无有效角色或授权时对应数组为空。身份每次从当前数据库记录汇总，不复用登录时的权限快照。这是对返回语义的实现补充，尚待真实调用方联调核对，不表示完整 D 模块已经完成。

D 会话校验实现：S02 请求体严格为 {}，从 Authorization 的 Bearer 凭据计算摘要并查询已有会话，拒绝未知、已撤销及不在 [createdAt, expiresAt) 内的凭据；读取身份不会延长有效期。前端与可信模块的 USER 转发复用共同来源检查，SYSTEM 不可查询用户身份。同步数据库访问在工作线程执行，连接或连接池超时返回 UNAVAILABLE，不返回空身份。此处接入现有会话表和身份汇总，不新增公开字段。

D 退出实现细化：S03 仅接收非空白 requestId，目标取自原 Bearer 凭据；采用凭据摘要限定的条件更新，事务提交后返回 loggedOut=true，重复请求不改写首次撤销时间，不影响同账号或其他账号的其他会话。已到期或账号已停用仍可退出；格式正确但查无记录的凭据也安全确认，不新建会话。缺失或格式不合法的凭据返回 UNAUTHENTICATED。输入没有其他可变业务字段，按原凭据的单向撤销自然满足重复语义；未知字段按公共规则拒绝。撤销时间取当前时间与创建时间的较晚者，避免异常测试时钟下违反时间约束或恢复会话。存储失败不确认退出，保留原请求编号核实；真实并发测试检查重复登录、重复退出以及退出与旧登录重试竞争。这些是本模块实现细化，公开字段保持不变，实际跨模块联调仍待完成。

scope 为 GLOBAL 时 id=null，其他范围填写对应院系或空间标识。权限不存在拒绝请求；权限不足则正常返回 allowed=false；未指定目标时只表示存在某个授权范围。业务关系由 M1、M2、M3 检查。

D 权限数据准备：空间归属与 SYSTEM 授权使用版本化迁移持久化，测试初始化读取共同 bootstrap 的 spaces、systemGrants，不另造教室编号或后台权限。后台主体按可信来源模块 m1/m2/m3 区分，授权独立于用户角色；已有空间不覆盖，已有后台主体连同授权整体跳过，保留停用和已撤销授权。初始化新增授权时检查权限目录允许的范围与真实院系/空间引用。S04 已使用这层数据，S06 查询接口和座位初始化仍待实现；后台拥有授权记录不等于已取得任何尚未实现的接口能力。

D-S04 实现细化：仅接受可信 M1/M2/M3 调用。USER 的 subjectId 为已核验凭据对应的 userId，重新读取当前有效角色授权；SYSTEM 的 subjectId 为核验后的来源模块小写 m1/m2/m3，独立读取后台授权，不能混用用户角色或在用户认证失败后降级。未知权限或不存在的目标院系/空间返回 NOT_FOUND；targetScope 可省，显式 null、非法范围结构及自报 userId/sourceModule/grants 等字段返回 INVALID_ARGUMENT。GLOBAL 覆盖全部，DEPARTMENT 覆盖同一院系及当前归属该院系的空间，SPACE 仅覆盖同一空间，不向上扩大；学校空间不归任一院系覆盖。allowedScopeTypes 限定授权记录的范围类型，GLOBAL 授权仍可判断具体院系或空间目标。

allowedScopes 返回该主体对所查权限当前全部有效的原始授权范围，去重后按类型、标识排序，不展开 GLOBAL、不因目标筛选列表；allowed 才表示指定目标是否命中。省略 targetScope 时 allowed 仅表示至少有一个有效范围。停用角色不授权，停用用户拒绝认证；后台主体未登记或停用正常返回 allowed=false 和空范围；已撤销或权限目录不再允许、引用已不存在的授权范围不生效。reason 为 GRANTED、NO_GRANT、OUT_OF_SCOPE、SUBJECT_UNREGISTERED 或 SUBJECT_DISABLED。注销/停用空间仍保留历史权限范围，业务是否可操作由业务服务另查状态；用户所属院系不是隐式授权。每次查询读取数据库，读取失败返回 UNAVAILABLE，不能冒充无权限；此接口不写数据、不延长会话。这些为本模块实现补充，跨模块真实联调尚未完成。

### D-S05 角色授权 manage_roles(input)／M1 管理入口

输入包含 operation，执行下列动作；变更另带 requestId，修改已有资料再带 expectedVersion。

| operation | 其余输入 | 输出 |
| --- | --- | --- |
| LIST_PERMISSIONS | {limit?:int,cursor?:str} | 列表单项 {code:str,name:str,allowedScopeTypes:[GLOBAL/DEPARTMENT/SPACE]} |
| LIST_USERS | {departmentId?:str,accountStatus?:ACTIVE/DISABLED,limit?:int,cursor?:str} | 列表单项 {userId:str,displayName:str,departmentId:str/null,accountStatus:ACTIVE/DISABLED} |
| LIST_ROLES / GET_ROLE | 列表 {status?:ACTIVE/DISABLED,limit?:int,cursor?:str}；详情 {roleId:str} | 列表或单项 {roleId:str,name:str,status:ACTIVE/DISABLED,grants:与S02的grants数组一致,version:int} |
| GET_USER_ROLES | {userId:str} | {userId:str,roleIds:[str],version:int} |
| CREATE_ROLE | {name:str,grants:与S02的grants数组一致} | 新角色，格式同 GET_ROLE |
| UPDATE_ROLE / DISABLE_ROLE | 修改 {roleId:str,name:str,status:ACTIVE/DISABLED,grants:与S02的grants数组一致}；停用 {roleId:str} | 更新后角色，格式同 GET_ROLE |
| SET_USER_ROLES | {userId:str,roleIds:[str]} | 格式同 GET_USER_ROLES |

授权与角色集合完整替换，空数组清空。新角色为 ACTIVE；停用角色保留历史但不再授权。未知权限、重复授权、无效范围或新增停用角色关联拒绝。已知用户无角色时查询返回空数组和 version=1，修改前先查询版本。

## 2. 主数据

### D-S06 查询资料 query_master(input)／M1、M2、M3

输入包含 operation；列表可带 limit、cursor，详情可带 includeUnregistered（bool，默认 false）。

| operation | 其余输入 | 输出 |
| --- | --- | --- |
| LIST_DEPARTMENTS | 无其他业务字段 | 列表单项 {departmentId:str,name:str} |
| LIST_SPACES / GET_SPACE | 列表可省 {departmentId:str,ownerKind:SCHOOL/DEPARTMENT,isStudyRoom:bool,enabled:bool,includeUnregistered:bool}；详情 {spaceId:str} | 列表或单项 {spaceId:str,name:str,location:str,isStudyRoom:bool,ownerKind:SCHOOL/DEPARTMENT,departmentId:str/null,registered:bool,enabled:bool,version:int} |
| LIST_SEATS / GET_SEAT | 列表可省 {spaceId:str,departmentId:str,enabled:bool,socketTag:FIXED/RAIL,includeUnregistered:bool}；详情 {seatId:str} | 列表或单项 {seatId:str,seatCode:str,spaceId:str,registered:bool,enabled:bool,socketTags:[FIXED/RAIL],version:int} |

列表筛选均可省。只返回授权范围的主数据，不判断预约时段；spaceId 在各模块一致。院系空间须有 departmentId，学校空间为 null。

### D-S07 维护资料 maintain_master(input)／M1 管理入口

输入包含 operation、requestId；修改与注销另带 expectedVersion。

| operation | 其余输入 | 输出 |
| --- | --- | --- |
| CREATE_SPACE | {name:str,location:str,isStudyRoom:bool,ownerKind:SCHOOL/DEPARTMENT,departmentId:str/null,enabled:bool} | 新空间，格式同 GET_SPACE |
| UPDATE_SPACE | {spaceId:str} 加 CREATE_SPACE 全部可写字段 | 更新后空间 |
| UNREGISTER_SPACE | {spaceId:str} | registered=false 的空间 |
| CREATE_SEAT | {seatCode:str,spaceId:str,enabled:bool,socketTags:[FIXED/RAIL]} | 新座位，格式同 GET_SEAT |
| UPDATE_SEAT | {seatId:str,seatCode:str,enabled:bool,socketTags:[FIXED/RAIL]} | 更新后座位 |
| UNREGISTER_SEAT | {seatId:str} | registered=false 的座位 |

座位号唯一且不复用；不能直接换座位所属空间。空间仍有登记座位时拒绝注销。注销保留历史引用，不清除预约或维修限制；业务影响由 M1 处理。M1管理入口在变更期间拒绝新的相关预约，重查现有预约，不允许破坏已有未终结预约；D仅接受M1的USER维护，不增加读取M1业务表或回调M1的接口。

### D-S08 查人员 query_people(input)／M2、M3

- 输入：{mode:CANDIDATES/BY_IDS,purpose:APPROVAL/DISPATCH/ESCALATION/ACCEPTANCE/DISPLAY,permissionCodes:[str],targetScope:与S04一致,userIds?:[str],limit?:int,cursor?:str}。
- 输出：CANDIDATES 返回分页；BY_IDS 返回 {people:[单项]}。单项为 {userId:str,displayName:str,departmentId:str/null,accountStatus:ACTIVE/DISABLED,eligible:bool/null,matchedPermissionCodes:[str]}。
- BY_IDS 必填 userIds，顺序与输入一致；候选具备全部所需权限。DISPLAY 仅 BY_IDS，permissionCodes=[]、eligible=null；其他用途权限非空。无资格与人员不存在分别报告，不返回联系方式或业务工作量。

## 3. 通知与审计

### D-S09 发通知 send_notification(input)／M1、M2、M3

- 输入：{requestId:str,sourceModule:M1/M2/M3,businessId:str,spaceId:str/null,recipientUserIds:[str],kind:下表值,title:str,body:str}。
- 输出：{notificationId:str,requestId:str,sourceModule:str,businessId:str,spaceId:str/null,kind:str,channel:EMAIL,title:str,body:str,createdAt:time,deliveries:[{recipientUserId:str,status:PENDING/SENDING/SUBMITTED/FAILED/UNKNOWN,attemptCount:int,lastAttemptAt:time/null,reason:str}]}。

| 来源 | kind |
| --- | --- |
| M1 | M1_BEFORE_START、M1_NOT_CHECKED_IN、M1_AUTO_CANCELLED、M1_MAINTENANCE_CANCELLED |
| M2 | M2_APPROVAL_PENDING、M2_APPROVED、M2_REJECTED、M2_TIMEOUT_CANCELLED、M2_CANCELLED |
| M3 | M3_ASSIGNED、M3_REPAIR_STARTED、M3_TIMEOUT_ESCALATED、M3_ACCEPTANCE_PENDING、M3_ACCEPTANCE_RESULT、M3_CANCELLED |

来源必须匹配可信模块。收件人非空、不重复；传用户标识，不传邮箱。受理不等于送达，SUBMITTED 仅渠道接受；地址缺失、账号禁用分别返回失败原因。初始额外重试上限为 2；仅明确暂时失败可有限重试，UNKNOWN 不自动重发。

### D-S10 查通知 get_notification(input)／原模块、授权管理入口

- 输入：{notificationId?:str,requestId?:str}，必须且只能一个。
- 输出：与 S09 一致，投递状态为查询时结果。
- 原模块只能查自己的通知；管理查询受权限范围限制。没查到不代表原请求永远不会受理。

### D-S11 操作审计 audit(input)／M1、M2、M3、授权管理入口

输入包含 operation。

| operation | 其余输入 | 输出 |
| --- | --- | --- |
| APPEND | {requestId:str,sourceModule:M1/M2/M3,actorType:USER/SYSTEM/UNKNOWN,actorId:str/null,objectType:str,businessId:str,spaceId:str/null,action:str,occurredAt:time,result:SUCCESS/REJECTED/FAILED/UNKNOWN,summary:str} | {auditId:str} 加已记录输入及 {verifiedBy:D/M1/M2/M3} |
| GET | {auditId?:str,requestId?:str,sourceModule?:M1/M2/M3/D}；编号必须且只能一个 | 与 APPEND 输出一致 |
| LIST | 可省 {sourceModule:M1/M2/M3/D,businessId:str,objectType:str,action:str,actorUserId:str,spaceId:str,timeFrom:time,timeTo:time,limit:int,cursor:str} | 分页，单项同 APPEND 输出 |

来源不可伪造；actorType 为 USER 时 actorId 是用户标识，SYSTEM 为来源模块，UNKNOWN 为 null。USER 模式只能追加当前已核验用户，verifiedBy=D；可信后台补报本模块先前核验的用户事件时 verifiedBy 为该模块。原模块可查自己的事件，跨来源须管理查看权限。不公开修改或删除。

## 4. 运行设置

### D-S12 公共设置 manage_public_config(input)／M1 管理入口

- 查询输入：{operation:GET}。
- 更新输入：{operation:UPDATE,authMethod:PASSWORD,notificationChannel:EMAIL,notificationRetryLimit:int,expectedVersion:int,requestId:str}。
- 输出：{authMethod:PASSWORD,notificationChannel:EMAIL,notificationRetryLimit:int,version:int}。
- 重试上限范围 0—5、初始 2，表示额外尝试次数；每条通知沿用受理时上限。新设置不重发旧通知，不暴露秘密。审批、维修、预约参数不放在 D。

## 5. 调用权限

用户身份与模块身份分别核验，后台不能代替用户管理资料。下列编码是 D v1 固定权限，业务模块自己的编码另行登记。

| 操作 | 所需用户权限 |
| --- | --- |
| S05 查询／变更 | d.rbac.read / d.rbac.manage |
| S06／S07 | d.master.read / d.master.manage |
| S08 | d.people.read |
| S09／S10 管理查询 | d.notify.send / d.notify.read |
| S11 追加／管理查询 | d.audit.append / d.audit.read |
| S12 查询／变更 | d.config.read / d.config.manage |

S05、S07、S12 仅 M1 的 USER 调用；S08 仅 M2、M3，可由获授权后台查询。S04、S06、S09、S11 可由授权模块后台调用，S10 与 S11 的原模块查询限可信后台、限制到本模块范围；任何管理查询仍核验当前用户。USER 同时核验模块访问资格、用户权限与范围；SYSTEM 按模块权限与范围判定，不能取得 USER 限定能力，失败不降级放行。

S10管理查看、S11的GET/LIST也允许有对应权限的管理用户直接USER调用；前端不携带模块凭据。S11 APPEND仍仅限可信来源模块，直接前端不得伪造sourceModule追加事件。

角色配置和公共运行设置限定全局权限；其他操作按教室或院系范围，spaceId=null 的通知与审计要求全局权限。初始用户、院系、角色与业务权限目录通过项目初始化准备，不增加注册接口。

验收重点：身份与越权、唯一编号、重复请求、版本冲突、真实投递、授权审计、失败与未知结果。初始账号、权限、凭据时限与通知设施按开发指导v1及共同测试输入实现；D负责人仍须组长安排，接口不由各agent自行改名。
