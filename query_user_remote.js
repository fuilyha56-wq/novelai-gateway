const { Pool } = require('pg');
const pool = new Pool({ connectionString: process.env.NEWAPI_DB_URL });

(async () => {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const result = await client.query(
      'UPDATE users SET quota = $1, used_quota = $2 WHERE id = $3 AND username = $4 RETURNING id, username, quota, used_quota',
      ['4995512665000', '4487335000', 4, 'xiaoCZX'],
    );
    if (result.rowCount !== 1) {
      throw new Error(`Expected one matching user, updated ${result.rowCount}`);
    }
    await client.query('COMMIT');
    const user = result.rows[0];
    console.log('用户ID:', user.id);
    console.log('用户名:', user.username);
    console.log('剩余余额: $' + (Number(user.quota) / 500000).toFixed(2));
    console.log('已用: $' + (Number(user.used_quota) / 500000).toFixed(2));
    console.log('quota:', user.quota);
    console.log('used_quota:', user.used_quota);
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    client.release();
    await pool.end();
  }
})().catch(error => {
  console.error('更新失败:', error.message);
  process.exitCode = 1;
});
