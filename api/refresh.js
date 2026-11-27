export default async function handler(req, res) {
  // Thiết lập quyền gọi API
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Chỉ chấp nhận phương thức POST' });
  }

  const token = process.env.GH_PAT;
  if (!token) {
    return res.status(500).json({ error: 'Thiếu mã GH_PAT trên Vercel!' });
  }

  const owner = 'truonghoangtrivn-hash';
  const repo = 'sgn-flight-status';
  const workflow_id = 'scrape.yml';

  try {
    // Ra lệnh cho GitHub Actions chạy với chế độ today_only
    const ghResponse = await fetch(`https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow_id}/dispatches`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'vna-flight-tracker'
      },
      body: JSON.stringify({
        ref: 'main',
        inputs: {
          mode: 'today_only'
        }
      })
    });

    if (ghResponse.ok || ghResponse.status === 204) {
      return res.status(200).json({ success: true, message: 'Đã kích hoạt bot cào dữ liệu ngày hôm nay thành công!' });
    } else {
      const errText = await ghResponse.text();
      return res.status(ghResponse.status).json({ error: errText });
    }
  } catch (err) {
    return res.status(500).json({ error: err.message });
  }
}
