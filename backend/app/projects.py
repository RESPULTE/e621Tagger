"""Project identity and deletion guards shared by reads and transactional writes."""

from fastapi import HTTPException


async def require_active_project(conn, project_id):
    row = await (await conn.execute(
        "SELECT 1 FROM projects WHERE id=? AND NOT EXISTS "
        "(SELECT 1 FROM deleted_projects WHERE project_id=projects.id)", (project_id,),
    )).fetchone()
    if not row:
        raise HTTPException(404, "Project not found")


async def require_project(db, project_id):
    # Resolve legacy duplicates and check deletion in one consistent read.
    project = await db.one(
        "SELECT p.* FROM projects requested LEFT JOIN canonical_projects k "
        "ON k.target_kind=requested.target_kind AND k.target_id=requested.target_id "
        "JOIN projects p ON p.id=coalesce(k.project_id,requested.id) "
        "WHERE requested.id=? AND NOT EXISTS "
        "(SELECT 1 FROM deleted_projects WHERE project_id IN (requested.id,p.id))", (project_id,),
    )
    if not project:
        raise HTTPException(404, "Project not found")
    return project
