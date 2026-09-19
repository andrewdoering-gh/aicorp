$headers = @{
  Authorization = "Bearer $token"
}

Invoke-RestMethod -Uri "http://127.0.0.1:8081/approval-requests" -Headers $headers | ConvertTo-Json -Depth 10