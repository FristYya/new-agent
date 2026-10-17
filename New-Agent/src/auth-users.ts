// Node.js 内置密码模块：
// - randomBytes：生成密码盐值（salt）；
// - randomUUID：生成全局唯一的用户 ID 和临时文件名；
// - scrypt：执行密码派生算法；
// - timingSafeEqual：用常量时间比较两个密码结果，降低时序攻击风险。
import {
  randomBytes,
  randomUUID,
  scrypt as scryptCallback,
  timingSafeEqual,
} from "node:crypto";

// file 存储模式所需的文件系统 API。
// MySQL 模式不会使用这些方法，但为了让同一套课程代码同时支持两种存储模式，
// 文件回退能力仍然保留在 UserRegistry 中。
import {
  mkdir,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";

// dirname() 用于从 dataFile 中取得父目录，写文件前先确保目录存在。
import { dirname } from "node:path";

// UserRegistry 不直接拼接 SQL，而是调用 mysql.ts 暴露出的三个能力：
// - insertAuthUser：向 users 表插入一个新用户；
// - readAuthUsers：读取 users 表中的全部注册用户；
// - usesMysqlStorage：判断当前启用的是 MySQL 还是 file 存储模式。
import {
  insertAuthUser,
  readAuthUsers,
  usesMysqlStorage,
} from "./storage/mysql.ts";

/**
 * 允许返回给浏览器的用户公开信息。
 *
 * 不包含 password 和 passwordHash。
 * 注册接口、登录接口即使直接返回 AuthUser，也不会把密码相关字段传给前端。
 */
export type AuthUser = {
  /** 服务端生成的稳定用户 ID，后续会作为会话 owner。 */
  id: string;

  /** 登录用户名，同时也是 users 表中的唯一字段。 */
  username: string;

  /** 页面上展示的名称，不参与登录校验。 */
  displayName: string;
};

/**
 * UserRegistry 在内存中保存的完整用户结构。
 *
 * 它同时兼容两种用户来源：
 * 1. 兼容预先配置的演示账号：可能只有明文 password；
 * 2. 通过注册接口创建的正式账号：一定保存 passwordHash 和 createdAt。
 */
type UserCredential = AuthUser & {
  /** 仅兼容内置演示账号，注册得到的账号不会写入该字段。 */
  password?: string;

  /** scrypt 计算后的编码结果，数据库和 JSON 文件实际保存的是这个字段。 */
  passwordHash?: string;

  /** 注册时间，使用 ISO 8601 字符串保存。 */
  createdAt?: string;
};

/**
 * 可以真正持久化的注册用户结构。
 * 与 UserCredential 不同，这里的 passwordHash 和 createdAt 都是必填字段，
 * 因为新注册用户在写入数据库或文件前必须已经完成密码哈希。
 */
type PersistedUser = AuthUser & {
  passwordHash: string;
  createdAt: string;
};

/**
 * 用户注册过程中可预期的业务错误。
 *
 * 路由层可以通过 statusCode 区分：
 * - 400：用户名、密码或显示名称不符合要求；
 * - 409：用户名已经存在。
 *
 * 这样业务错误不会被统一当成 500 服务器错误返回。
 */
export class UserRegistryError extends Error {
  constructor(
    /** 返回给调用方的可读错误信息。 */
    message: string,

    /** 默认按参数错误处理，特殊场景可以传入 409。 */
    readonly statusCode = 400,
  ) {
    super(message);

    // 显式设置错误名称，便于日志和调试工具识别错误类型。
    this.name = "UserRegistryError";
  }
}

/**
 * 把 Node.js 回调风格的 scrypt() 包装成 Promise。
 *
 * 这样 hashPassword() 和 verifyHashedPassword() 就可以使用 await，
 * 密码计算失败时也会自然进入 async/await 的异常处理流程。
 */
function scrypt(
  /** 用户输入的原始密码，仅参与本次计算，不会直接落库。 */
  password: string,

  /** 当前用户独有的随机盐值。 */
  salt: Buffer,
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    scryptCallback(
      password,
      salt,
      // 输出 64 字节派生密钥；注册和登录必须使用相同长度。
      64,
      (error, derivedKey) => {
        // Node 回调中的错误转换成 Promise rejection。
        if (error) reject(error);
        // 成功时返回派生密钥 Buffer。
        else resolve(derivedKey);
      },
    );
  });
}

/**
 * 为新注册用户生成可持久化的密码哈希字符串。
 * 最终格式为：scrypt$<base64url salt>$<base64url hash>。
 */
async function hashPassword(password: string): Promise<string> {
  // 为每个账号生成独立的 16 字节随机 salt。
  // 即使两个用户设置了相同密码，最终存储结果也会不同。
  const salt = randomBytes(16);

  // scrypt 是计算成本较高的密码派生算法，比直接使用普通哈希更适合保存密码。
  const derivedKey = await scrypt(password, salt);

  // 把算法标识、salt 和派生密钥一起编码。
  // 登录时会拆出同一个 salt，再对本次输入的密码重新计算并比较。
  // base64url 只负责安全编码二进制数据，并不是额外的加密算法。
  return `scrypt$${salt.toString("base64url")}$${derivedKey.toString("base64url")}`;
}

/**
 * 校验用户输入的密码是否与已保存的 scrypt 编码结果一致。
 *
 * 无论编码格式不合法还是计算过程中出现异常，都返回 false，
 * 避免登录接口因为一条损坏的数据直接报 500。
 */
async function verifyHashedPassword(
  password: string,
  encoded: string,
): Promise<boolean> {
  // 按注册时约定的格式拆分算法、salt 和哈希结果。
  const [algorithm, saltText, hashText] = encoded.split("$");

  // 非 scrypt 格式或缺少任一部分，都不是本系统可验证的合法密码记录。
  if (algorithm !== "scrypt" || !saltText || !hashText) {
    return false;
  }

  try {
    // expected 是注册时存下来的正确结果。
    const expected = Buffer.from(hashText, "base64url");

    // actual 是使用“本次输入密码 + 原 salt”重新计算出来的结果。
    const actual = await scrypt(
      password,
      Buffer.from(saltText, "base64url"),
    );

    // timingSafeEqual 要求两个 Buffer 长度一致，因此先判断长度。
    // 长度一致后使用常量时间比较，避免普通字符串比较可能泄露的时序信息。
    return expected.length === actual.length &&
      timingSafeEqual(expected, actual);
  } catch {
    // base64url 解码或 scrypt 计算异常都视为密码不匹配。
    return false;
  }
}

/**
 * 校验内置演示账号的明文密码。
 *
 * 该分支只是为了兼容成品中可能通过配置注入的演示账号；
 * 通过注册接口创建的账号不会以明文形式保存密码。
 */
function verifyPlainPassword(
  input: string,
  expected: string,
): boolean {
  // 将两个字符串转成 Buffer，才能使用 timingSafeEqual()。
  const inputBuffer = Buffer.from(input);
  const expectedBuffer = Buffer.from(expected);

  // 长度不一致直接失败；长度一致时再做常量时间比较。
  return inputBuffer.length === expectedBuffer.length &&
    timingSafeEqual(inputBuffer, expectedBuffer);
}

/**
 * 把内部 UserCredential 转换为允许暴露给前端的 AuthUser。
 */
function publicUser(user: UserCredential): AuthUser {
  // 这里显式挑选三个公开字段，而不是使用对象展开运算符。
  // 将来即使 UserCredential 新增 token、手机号等敏感字段，也不会被意外返回。
  return {
    id: user.id,
    username: user.username,
    displayName: user.displayName,
  };
}

/** 校验并限制用户名格式。 */
function validateUsername(username: string): void {
  // \p{L} 支持中英文等 Unicode 字母，\p{N} 支持 Unicode 数字。
  // 用户名总长度限制为 3～64 位，并允许点、横线和下划线。
  if (!/^[\p{L}\p{N}_.-]{3,64}$/u.test(username)) {
    throw new UserRegistryError(
      "用户名须为 3–64 位，只能包含中英文、数字、点、横线或下划线",
    );
  }
}

/** 校验密码长度，避免过短密码和超大请求造成不必要的计算压力。 */
function validatePassword(password: string): void {
  if (password.length < 8 || password.length > 128) {
    throw new UserRegistryError("密码长度须为 8–128 位");
  }
}

/**
 * 用户注册表：统一管理用户加载、查询、密码验证和注册持久化。
 *
 * 路由层只需要调用这些公开方法，不需要关心当前使用 MySQL 还是 JSON 文件。
 */
export class UserRegistry {
  /**
   * 以 username 为 key 的内存索引。
   * 登录和用户名查重都可以 O(1) 查询，无须每次访问数据库或遍历数组。
   */
  private readonly users = new Map<string, UserCredential>();

  /**
   * 仅保存需要持久化的注册用户，不包含内置演示账号。
   * file 模式写 JSON 文件时会使用这个数组。
   */
  private readonly persistedUsers: PersistedUser[] = [];

  // 同一 Node.js 进程中的注册任务会依次追加到这个 Promise 队列。
  // 这样两个同名注册请求不会同时通过 this.users.has() 的检查。
  // 多进程之间仍由 users.username 的数据库唯一索引最终兜底。
  private mutationQueue: Promise<void> = Promise.resolve();

  /**
   * 构造函数保持 private，强制调用方使用异步的 create()。
   * 因为实例可用前必须先从数据库或文件加载已有用户。
   */
  private constructor(
    /** file 模式用户 JSON 文件的位置；MySQL 模式下仍保留该配置。 */
    private readonly dataFile: string,

    /** 由服务端配置传入的内置演示账号。 */
    builtInUsers: UserCredential[],
  ) {
    // 先把内置账号加入内存索引。
    for (const user of builtInUsers) {
      this.users.set(user.username, user);
    }
  }

  /**
   * 创建并初始化 UserRegistry。
   *
   * 初始化顺序：
   * 1. 放入内置演示账号；
   * 2. 根据当前存储模式读取注册用户；
   * 3. 过滤损坏数据和重名数据；
   * 4. 建立内存 username 索引。
   */
  static async create(
    dataFile: string,
    builtInUsers: UserCredential[] = [],
  ): Promise<UserRegistry> {
    const registry = new UserRegistry(dataFile, builtInUsers);

    try {
      // 两种存储模式最终都整理为 { users: PersistedUser[] }，
      // 后面的校验和加载逻辑因此只需要写一份。
      const parsed: { users?: PersistedUser[] } = usesMysqlStorage()
        // MySQL 模式：从独立 users 表逐行读取，并包装成相同对象结构。
        ? { users: await readAuthUsers() }
        // file 模式：读取并解析 data/auth-users.json 的完整 JSON 文档。
        : JSON.parse(
            await readFile(dataFile, "utf-8"),
          ) as { users?: PersistedUser[] };

      // 把持久化用户逐个恢复到内存。
      for (const user of parsed.users ?? []) {
        // 任一关键字段缺失，都说明这条持久化数据不完整。
        // 跳过坏数据可以避免整个服务因单条异常记录而无法启动。
        if (
          !user?.id ||
          !user?.username ||
          !user?.displayName ||
          !user?.passwordHash
        ) continue;

        // 内置账号优先：如果用户名已经被内置账号占用，则不覆盖它。
        if (registry.users.has(user.username)) continue;

        // persistedUsers 服务于 file 模式后续的全量写回。
        registry.persistedUsers.push(user);

        // users Map 服务于运行期登录查询和注册查重。
        registry.users.set(user.username, user);
      }
    } catch (error: any) {
      // file 模式第一次运行时 auth-users.json 可能尚不存在，ENOENT 属于正常状态。
      // MySQL 查询错误、JSON 格式错误等其他问题必须抛出，避免服务在数据未正确加载时运行。
      if (error?.code !== "ENOENT") {
        throw new Error(
          `读取注册用户失败：${error?.message || error}`,
        );
      }
    }

    // 到这里说明初始化成功，调用方可以开始处理注册和登录请求。
    return registry;
  }

  /** 按用户名查找用户，主要用于登录和注册查重。 */
  findByUsername(username: string): UserCredential | undefined {
    return this.users.get(username);
  }

  /**
   * 按用户 ID 查找用户，主要用于根据登录 Cookie 恢复当前用户。
   * users Map 以 username 建索引，因此这里需要遍历 Map 中的值。
   */
  findById(id: string): UserCredential | undefined {
    return [...this.users.values()].find(
      (user) => user.id === id,
    );
  }

  /** 返回不含凭据的用户列表，供后台任务恢复等服务端流程使用。 */
  list(): AuthUser[] {
    return [...this.users.values()].map(({ id, username, displayName }) => ({
      id,
      username,
      displayName,
    }));
  }

  /**
   * 统一验证密码。
   * 注册用户优先走安全的 scrypt 哈希校验；只有内置演示账号才回退到明文兼容逻辑。
   */
  async verifyPassword(
    user: UserCredential,
    password: string,
  ): Promise<boolean> {
    if (user.passwordHash) {
      return verifyHashedPassword(password, user.passwordHash);
    }

    return verifyPlainPassword(password, user.password ?? "");
  }

  /**
   * 注册一个新用户，并将其保存到当前启用的存储后端。
   * 成功时只返回 AuthUser 公开字段；失败时抛出 UserRegistryError 或存储异常。
   */
  async register(
    rawUsername: string,
    password: string,
    rawDisplayName?: string,
  ): Promise<AuthUser> {
    // 去掉用户输入首尾的空白，避免“alice”和“ alice ”被当作两个账号。
    const username = rawUsername.trim();

    // 没有填写显示名称时默认使用用户名，并同样清理首尾空白。
    const displayName = (rawDisplayName || username).trim();

    // 在进行昂贵的密码哈希和数据库写入前，先完成同步参数校验。
    validateUsername(username);
    validatePassword(password);

    // 显示名称至少包含一个非空白字符，最大长度为 64 位。
    if (!displayName || displayName.length > 64) {
      throw new UserRegistryError("显示名称长度须为 1–64 位");
    }

    // 当前注册操作追加到 mutationQueue 尾部。
    // 前一个任务即使失败，catch() 也会把队列恢复成可继续执行的状态。
    const task = this.mutationQueue
      .catch(() => undefined)
      .then(async () => {
        // 进入串行区后再次检查用户名，避免并发请求之间产生竞态条件。
        if (this.users.has(username)) {
          throw new UserRegistryError("该用户名已存在", 409);
        }

        // 构造准备持久化的新用户记录。
        const record: PersistedUser = {
          // 添加 user_ 前缀，日志和数据库中更容易识别 ID 类型。
          id: `user_${randomUUID()}`,
          username,
          displayName,

          // 数据库或 JSON 文件只接收哈希结果，不接收原始密码。
          passwordHash: await hashPassword(password),

          // 业务层统一保存 UTC ISO 时间，展示时再由前端转换为本地时区。
          // MySQL 适配器在执行 INSERT 前会把这个字符串转换为 Date。
          createdAt: new Date().toISOString(),
        };

        // 先暂存到内存，使保存成功后的实例立即可以登录和查找该用户。
        this.persistedUsers.push(record);
        this.users.set(username, record);

        try {
          if (usesMysqlStorage()) {
            // MySQL 模式只插入当前记录，不需要重写其他用户。
            await insertAuthUser(record);
          } else {
            // file 模式把 persistedUsers 数组原子写回 JSON 文件。
            await this.persistFile();
          }
        } catch (error) {
          // 持久化失败必须同步回滚两个内存容器。
          // 否则用户会在当前进程中看似注册成功，但重启后彻底丢失。
          this.users.delete(username);
          this.persistedUsers.pop();

          // MySQL 唯一索引发现重复用户名时会返回 ER_DUP_ENTRY。
          // 把数据库错误转换为路由层能够理解的 409 业务错误。
          if ((error as { code?: string })?.code === "ER_DUP_ENTRY") {
            throw new UserRegistryError("该用户名已存在", 409);
          }

          // 其他数据库或文件错误保持原样抛出，交给统一错误处理中间件记录。
          throw error;
        }

        // 永远只返回公开字段，passwordHash 不会离开服务端。
        return publicUser(record);
      });

    // 队列只关心“上一个任务是否结束”，不保留其返回值或失败状态。
    // 同时 register() 仍然返回原始 task，让当前请求能够收到真实结果或异常。
    this.mutationQueue = task.then(
      () => undefined,
      () => undefined,
    );
    return task;
  }

  /**
   * 以“临时文件 + rename”的方式原子更新 file 模式用户文件。
   *
   * 如果进程在写临时文件时退出，旧文件仍然保持完整；
   * 只有临时文件成功写完后，rename() 才会一次性替换正式文件。
   */
  private async persistFile(): Promise<void> {
    // 第一次启动时 data 目录可能不存在，recursive 可递归创建父目录。
    await mkdir(dirname(this.dataFile), { recursive: true });

    // 进程 ID + UUID 可以避免同机多个进程使用相同临时文件名。
    const temporaryFile =
      `${this.dataFile}.${process.pid}.${randomUUID()}.tmp`;

    try {
      // 先把完整用户数组写入临时文件。
      await writeFile(
        temporaryFile,
        // 两个空格缩进便于人工查看，末尾换行符合普通文本文件习惯。
        `${JSON.stringify({ users: this.persistedUsers }, null, 2)}\n`,
        // 0o600 表示仅当前系统用户可读写，减少凭证文件被其他账号读取的风险。
        { encoding: "utf-8", mode: 0o600 },
      );

      // 写入完整后再替换正式文件，避免产生只写了一半的 JSON。
      await rename(temporaryFile, this.dataFile);
    } catch (error) {
      // 写入或重命名失败时尽力删除临时文件。
      // 清理本身失败不应覆盖最初的持久化异常。
      await rm(temporaryFile, { force: true })
        .catch(() => undefined);

      // 把原始错误继续抛给 register()，由它回滚内存状态。
      throw error;
    }
  }
}
