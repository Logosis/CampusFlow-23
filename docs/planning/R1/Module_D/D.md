# D：公共层接口 v1

本版固定服务编号、函数名、输入输出；D-F01 身份权限、D-F02 空间座位、D-F03 通知、D-F04 审计范围不变。依据：[任务书](../../../../Pj-任务书.pdf)第 1 页。

**状态：开发中。D-S01 登录、D-S02 当前身份、D-S03 退出、D-S04 权限判断、D-S06 主数据查询、D-S08 人员查询、D-S09 通知受理、D-S10 通知查询与 D-S12 公共设置已提供实现和测试代码；通知后台邮件投递已通过本地PostgreSQL与Mailpit验收；其他公开接口尚未实现，完整模块验收与跨模块联调尚未完成。** 初版采用账号密码登录、邮件通知；这是本组起始方案，后续调整须同步调用方。只约定公开接口，不设计内部表、类或任务。

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

D 权限数据准备：空间归属与 SYSTEM 授权使用版本化迁移持久化，测试初始化读取共同 bootstrap 的 spaces、systemGrants，不另造教室编号或后台权限。后台主体按可信来源模块 m1/m2/m3 区分，授权独立于用户角色；已有空间不覆盖，已有后台主体连同授权整体跳过，保留停用和已撤销授权。初始化新增授权时检查权限目录允许的范围与真实院系/空间引用。S04 与 S06 使用这层数据，座位初始化见下；后台拥有授权记录不等于已取得任何尚未实现的接口能力。

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

D 主数据准备：新增 seats 表并由版本化迁移建立到 spaces 的外键，初始化沿用共同 bootstrap 的三个座位，已有座位整体保留，重复初始化不重置登记、启用、插座或版本。座位号在所有现存记录（含已注销记录）中唯一，插座标签只接受 FIXED/RAIL 的无重复组合；新增登记座位必须引用已登记的真实空间。S06 查询已接入这些记录；S07 维护接口和改号后的历史占号规则仍待实现。

D-S06 实现细化：五种 operation 分别声明严格输入及完整输出，不能把列表筛选传给详情、传其他 operation 的字段或自报身份；可省字段不接受显式 null。仅可信 M1/M2/M3 可按 USER 或 SYSTEM 调用，前端不能直调。查询在同一数据库会话复用 S04 的当前有效授权计算，检查 d.master.read；没有有效范围返回 FORBIDDEN，存储故障返回 UNAVAILABLE，不伪装成空列表。USER 认证失败不转用 SYSTEM 权限。

授权范围在数据库筛选与分页前应用：GLOBAL 可读全部；DEPARTMENT 可读该院系及其当前所属空间、座位；SPACE 仅可读该空间及座位，不向上增加院系权限。因此仅有 SPACE 授权时 LIST_DEPARTMENTS 返回空列表；详情中仍保留该空间约定的 departmentId 字段。列表条件与范围取交集，无匹配时返回空列表；显式引用不存在的院系、空间或座位返回 NOT_FOUND，已存在且可见但超出授权范围的详情返回 FORBIDDEN。多个重叠范围不会重复列出同一记录。

includeUnregistered 默认 false，列表与详情均隐藏已注销记录；座位还要求所属空间未注销。传 true 后可查询授权范围内的历史记录，但不会增加权限。enabled 未传时包含启用和停用记录；空间列表的 enabled 筛选空间，座位列表的 enabled 只筛选座位自身，不替代调用方对所属空间状态的判断。socketTag 按数组包含筛选，socketTags 输出按 FIXED、RAIL 稳定排序；多个筛选条件同时生效。这些查询不检查预约、维修限制或时段可用性。

列表按各自稳定 ID 升序，limit 默认 20、范围 1—100；以最后一条 ID 续查，并多读一条判断是否有下一页。签名游标绑定 operation、所有有效筛选、limit、来源模块、主体及当前有效授权范围；条件、身份、范围或服务密钥改变后须从第一页重查，不匹配或被篡改返回 INVALID_ARGUMENT。游标不包含密码或会话凭据。每页重新校验权限并查询当前数据，不承诺跨页历史快照；期间发生新增、注销或归属变更时，需从第一页重查以得到最新完整列表。以上是 D 对已有契约的实现补充，未改公开字段或其他模块，真实跨模块联调仍待核对。

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

D-S08 实现细化：仅可信 M2/M3 可按 USER 或 SYSTEM 调用，先以当前主体的 d.people.read 校验完整 targetScope；没有覆盖该目标的权限返回 FORBIDDEN，不因模块有后台权限而放行失败的 USER。前端与 M1 不能直调。mode、purpose、permissionCodes、targetScope 均必填，BY_IDS 必填 userIds；仅 CANDIDATES 接受 limit/cursor，字段严格按类型校验，拒绝多余字段、显式 null 或空白标识。

资格采用与 S04 相同的范围覆盖规则及权限目录 allowedScopeTypes，只读取 ACTIVE 角色的当前授权；多项权限可以来自不同角色，但必须全部覆盖 targetScope。人员所属院系不是隐式授权，也不限制已获其他院系或 GLOBAL 授权的人选。重复权限编码按集合处理，matchedPermissionCodes 去重后按编码排序；有部分资格时仅返回已满足的编码。停用账号在资格核验中 eligible=false、matchedPermissionCodes=[]，不进入候选列表。DISPLAY 不进行资格判断，即使展示停用账号也保持 eligible=null、matchedPermissionCodes=[]，同时返回其 accountStatus。

BY_IDS 保留输入顺序和重复项；空 userIds 在调用权限及引用校验通过后返回 people=[]。任何用户不存在时整批返回 NOT_FOUND，不用 eligible=false 代替，也不返回部分结果；未知资格编码或目标范围引用同样返回 NOT_FOUND。CANDIDATES 只列启用且满足全部资格的人员，在数据库内先筛选再分页；多个有效角色或重叠范围不重复返回同一人，无合格人员正常返回空列表。查询仅投影约定的基本资料与资格，不返回登录账号、联系方式、密码哈希或会话凭据。

候选按 userId 升序，limit 默认 20、范围 1—100，复用签名游标绑定用途、有效资格编码集合、目标范围、limit、来源模块、主体和当前读取权限范围。条件不匹配或游标损坏返回 INVALID_ARGUMENT；调用权限全部撤销返回 FORBIDDEN。每页重新读取当前资格，停用或撤销立即影响后续查询，不承诺跨页历史快照。已保留的停用/注销空间仍可作为历史业务的资格目标，按其当前归属判断覆盖；是否允许新的业务、是否属于当前工单/申请、是否应排除承接人由 M2/M3 检查。D 不替代这些业务关系判断。存储失败返回 UNAVAILABLE，不伪装为空名单。上述为 D 的实现补充，未改变公开字段，真实调用方联调仍待完成。

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

D 通知数据准备：notifications 保存来源、原请求号、业务关联、内容、受理时间、输入快照和受理时的重试上限；notification_deliveries 按收件人保存顺序、地址、状态、实际尝试次数和时间。来源与请求号联合唯一，同一来源的 USER/SYSTEM 共用编号空间，不同来源可使用相同编号；每个通知中的收件人及顺序均唯一。初始化不预造通知，不覆盖已有记录。地址和内部重试、租约字段不进入 S09/S10 的公开返回。

通知来源与 kind 对应既有目录；请求收件人非空且无重复，title/body 非空白、最多2000字符，title不接受换行，body保留正文换行。S10仅接受一个查询编号，可省字段不接受显式null。发送接口同时核验可信来源、当前权限、真实引用和幂等输入，输入模型不替代来源认证。

持久化区分 PENDING、SENDING、SUBMITTED、FAILED、UNKNOWN。尝试次数为0时 lastAttemptAt=null；SENDING/SUBMITTED/UNKNOWN 必须已有一次实际尝试记录，不能把刚受理写成已提交。投递中保留单次处理令牌和有效租约，多进程互斥领取、超时后记录UNKNOWN且不自动重发。明确暂时失败才允许安排下次尝试；地址缺失或账号禁用可以在未尝试时记录FAILED。S09/S10已接入受理与查询，SMTP及后台任务已接入D入口；实际投递状态以持久记录为准，不能用代码已接入代替真实收信验收。

D-S09/S10 实现补充：S09仅可信M1/M2/M3可调用，sourceModule必须与可信来源一致，USER/SYSTEM分别按当前d.notify.send授权核验；spaceId=null要求GLOBAL权限，其他目标复用S04的范围覆盖及引用检查。已保留的停用/注销空间可用于历史业务通知。未知收件人整批NOT_FOUND且不留通知；已存在但停用、缺地址、地址不合法的收件人分别保存FAILED/ACCOUNT_DISABLED、EMAIL_MISSING、EMAIL_INVALID，其他人保存PENDING/QUEUED、attemptCount=0、lastAttemptAt=null；顺序与输入一致。邮箱采用单一ASCII addr-spec，不接受显示名、控制符或国际化地址，这是本版渠道能力补充。业务内容、收件人业务关系和通知触发时机仍由调用模块负责。

通知与全部投递记录同事务保存，提交确认后返回；事务级锁按可信来源及requestId串行化受理，同一来源的USER/SYSTEM共用编号空间。相同编号和完整输入（包括收件人顺序）复用原通知并返回当前投递状态，不重置状态、地址快照、次数或受理时重试上限；不同输入返回REQUEST_ID_REUSED。每次重复仍核验当前权限，失败不降级。未初始化配置返回UNAVAILABLE。存储或提交结果不确定时返回UNAVAILABLE，保留原编号核实；不因响应失败重新生成编号。

S10 SYSTEM仅查询本模块通知，按requestId也自动限定来源；其他模块的notificationId返回NOT_FOUND。USER（模块转发或直接管理查询）须有当前d.notify.read及通知目标范围权限，不以收件人身份代替管理授权。管理用户跨来源查requestId时，若多个可见通知重名，返回CONFLICT并要求改用notificationId；不可见通知不进入歧义判断。不存在返回NOT_FOUND，不代表仍在途的原请求永远不会受理。每次读取持久化的收件人状态，不输出邮箱、输入快照、重试或租约字段；查询不改变数据，也不发送邮件。当前注册9个接口，health剩S05/S07/S11；接入后台后workerConfigured=true，workerError报告最近一轮后台任务异常；后台投递与跨模块真实联调的结果分别记录。


D-S09后台投递补充（内部实现，不增加公开接口）：

- D启动时通过共同create_app的tick入口调度；同步数据库与SMTP放入工作线程，单轮最多尝试10个收件人，轮间休息1秒。多进程使用FOR UPDATE SKIP LOCKED逐条领取已到期PENDING；先提交SENDING、尝试次数、最后尝试时间、唯一令牌及120秒租约，再执行SMTP。网络调用期间不持有数据库事务或行锁。领取提交未确认时不发送；领取前再次检查账号启用状态，邮箱沿用受理快照。
- 每个收件人单独SMTP会话和MIME邮件，不把其他收件人放入To/Cc/Bcc。标题和正文UTF-8纯文本，正文保留换行；Message-ID按通知与收件人稳定生成，供追踪，不依赖邮件服务按此去重。最终DATA收到250才记录SUBMITTED/SMTP_ACCEPTED；不等于已进入真实用户收件箱。
- 明确4xx或正文发送前的连接失败可有限重试；延迟为5、10、20、40、80秒，受通知保存的0—5次额外上限约束，总尝试最多6次。明确5xx、证书错误、不支持所需TLS/认证则FAILED。自发起DATA起断线、超时或损坏响应保守记UNKNOWN；仅Python 3.12明确的DATA初始拒绝及收到的最终4xx/5xx按已拒绝处理。UNKNOWN、FAILED、SUBMITTED不再自动领取。
- 完成结果必须匹配SENDING及本次令牌；租约已到期则保守转UNKNOWN。进程退出或结果持久化失败后保留SENDING，租约到期转UNKNOWN，绝不把过期任务回收为PENDING；晚到回执不覆盖终态。停机取消调度不保证已进入线程的SMTP立即中止，这也是保留租约和UNKNOWN的原因。存储与配置异常使workerError=true并记录受控日志；日志含D/S09、可定位的通知/requestId及原因，不写邮箱、正文、凭据或原始SMTP异常。正常明确拒绝是该收件人的业务结果，不等同后台崩溃。
- 读取已有SMTP_HOST/PORT/FROM/USERNAME/PASSWORD；可选SMTP_SECURITY为none/starttls/ssl。APP_ENV=test且主机为127.0.0.1/localhost/::1/mailpit时默认none，仅无认证本地测试允许明文；其他主机默认STARTTLS并校验证书，不能自动降级。现有本地Mailpit的1025/8025配置可直接沿用，无需新增迁移或重复seed。
- 使用共同utc_now：固定TEST_CLOCK_FILE时，正常首发照常处理，但重试和租约到期随测试时钟推进，墙上时间流逝不改变它；不得为恢复邮件重置现有测试时钟或手工重排UNKNOWN。接口测试暂停真实后台轮询以保护开发数据；worker专项测试显式恢复真实业务，使用隔离PostgreSQL、独立连接并发及SMTP边界替身。真实Mailpit验收另检查持久状态、原始MIME和重复请求不增发。

实现依据：[Python 3.12 smtplib](https://docs.python.org/3.12/library/smtplib.html)、[SMTP最终响应语义](https://www.rfc-editor.org/rfc/rfc5321.html#section-4.2.5)。本地收信检查使用[Mailpit v1 API](https://mailpit.axllent.org/docs/api-v1/)，不调用邮件释放或转发接口。

本地验收记录（2026-10-21）：共同基础与D模块925项测试通过；实际D服务与Mailpit检查通过，原有6条及新增2条投递均为SUBMITTED，原始邮件内容和重复请求不增发检查通过。公共设置保持不变，测试会话已退出；这是D的本地通知链路验收，M1/M2/M3业务触发与整组跨模块联调仍待验证。

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

D-S12 实现细化：GET 与 UPDATE 使用严格判别输入，只接受 M1 转发的 USER；分别核验当前 d.config.read / d.config.manage 的 GLOBAL 权限，不能用局部范围或后台权限代替。不返回 SMTP、会话密钥、凭据或密码。公共设置保存在单条持久化记录，初始 PASSWORD、EMAIL 和重试上限读取共同 bootstrap.d，version=1；重复 seed 保留已有设置和版本。尚未初始化返回 UNAVAILABLE，不临时构造默认值。

更新锁定该配置行，等待后核验当前用户权限，再查询本服务的原请求结果、核对输入与版本。成功更新和原请求结果在同一事务保存，提交后才确认；新请求即使参数相同也增加一次版本。请求编号空间固定为 (m1, manage_public_config, requestId)，不同 M1 用户共用，仍各自需要当前管理权限。原请求且输入一致时返回当次保存的完整结果，不因旧 expectedVersion 拒绝、不覆盖后续版本；输入变化（含 expectedVersion）返回 REQUEST_ID_REUSED，新请求的版本不符返回 CONFLICT。拒绝或回滚的请求不留下成功记录。并发重复请求只生效一次，不同请求竞争同一版本只能一个成功。存储或提交失败返回 UNAVAILABLE，结果未确认时保留原请求编号，不自动换号重试。

S12只更新公共设置；S09受理时复制重试上限到通知，投递任务沿用该快照，旧通知不随设置改变，UNKNOWN不自动重发。S12不调用邮件设施、不修改既有通知、不增加公开字段；M1管理入口的真实联调仍待完成。

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
