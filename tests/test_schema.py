from dataclasses import fields, replace
import sqlite3

from support import *
from stylelora.project_db.errors import *
from stylelora.project_db.validation import dumps, relative_path, tags, integer
from stylelora.project_db.repositories import schemes


class SchemaTests(ProjectCase):
    def test_D01_exact_schema(self):
        dtos = {'sources':SourceRow,'source_items':SourceItemRow,'images':ImageRow,'annotations':AnnotationRow,
                'jobs':JobRow,'operation_log':OperationRow,'export_schemes':ExportSchemeRow,'import_schemes':ImportSchemeRow}
        def check(db):
            tables = db.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall()
            self.assertEqual({r[0] for r in tables},set(dtos))
            count = 0
            for table,dto in dtos.items():
                info = db.execute(f'PRAGMA table_info({table})').fetchall()
                self.assertEqual([r['name'] for r in info],[f.name for f in fields(dto)])
                self.assertEqual(info[0]['type'],'INTEGER')
                self.assertEqual(info[0]['pk'],1)
                count += len(info)
            self.assertEqual(count,93)
            indexes = db.execute("SELECT sql FROM sqlite_schema WHERE type='index'").fetchall()
            self.assertEqual(sum(r[0] is not None for r in indexes),15)
            self.assertEqual(sum(r[0] is None for r in indexes),2)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
        self.read(check)

    def populated(self):
        image = self.image(('a',))
        job = self.job((image.image_id,))
        self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,self.outcome(job,image)))
        plan = self.rule(image,('b',))
        self.write(lambda db: cmd.apply_rule(db,plan))
        return image,job

    def test_D02_all_not_null_and_enum_unknowns(self):
        image,job = self.populated()
        def check(db):
            for table in ('sources','source_items','images','annotations','jobs','operation_log','export_schemes','import_schemes'):
                for column in db.execute(f'PRAGMA table_info({table})').fetchall():
                    if column['notnull']:
                        with self.subTest(table=table,column=column['name']):
                            db.execute('SAVEPOINT probe')
                            with self.assertRaises(sqlite3.IntegrityError):
                                db.execute(f"UPDATE {table} SET {column['name']}=NULL")
                            db.execute('ROLLBACK TO probe')
                            db.execute('RELEASE probe')
            enums = {'sources':('type','cost_policy','status','locked'),'source_items':('aspect','status'),
                     'images':('review_status','rating','rating_source','rating_verified','file_missing'),
                     'annotations':('source','type','is_current'),'jobs':('executor','mode','status'),
                     'operation_log':('status',),'export_schemes':('exporter',),'import_schemes':('reader','is_default')}
            for table,columns in enums.items():
                for column in columns:
                    with self.subTest(table=table,column=column):
                        with self.assertRaises(sqlite3.IntegrityError):
                            db.execute(f'UPDATE {table} SET {column}=?',('unknown',))
            db.execute("UPDATE images SET rating='general',rating_source='detector',rated_at='clock' WHERE image_id=?",(image.image_id,))
        self.write(check)
        with self.assertRaises(ValidationError):
            self.write(lambda db: images.set_rating(db,image.image_id,expected=None,value=RatingValue('general','detector',False,'2026-01-01T00:00:00.000000Z')))

    def test_D03_foreign_keys_restrict(self):
        image,job = self.populated()
        def check(db):
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
            statements = [("UPDATE images SET source_id=999999",()),("UPDATE source_items SET image_id=999999 WHERE status='done'",()),
                          ("UPDATE annotations SET job_id=999999 WHERE source='wd'",()),('DELETE FROM sources',()),('DELETE FROM images',()),('DELETE FROM jobs',())]
            for sql,params in statements:
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql,params)
        self.write(check)
        self.assertEqual(self.read(lambda db: db.execute('PRAGMA foreign_keys').fetchone()[0]),1)

    def test_D04_unique_natural_keys_and_defaults(self):
        image = self.image()
        second,_ = self.source()
        def check(db):
            item = sources.list_items(db,ItemFilter(second.source_id),PageRequest()).items[0]
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO source_items(source_id,seq,prompt,updated_at) VALUES(?,1,'x','t')",(second.source_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE source_items SET status='done',image_id=? WHERE item_id=?",(image.image_id,item.item_id))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO import_schemes(name,is_default,created_at,updated_at) VALUES('x',1,'t','t')")
        self.write(check)

    def test_D04a_import_duplicate_skip(self):
        image = self.image()
        source,_ = self.source()
        item = self.items(source)[0]
        result = self.write(lambda db: cmd.skip_duplicate(db,item.item_id,sha256=image.sha256)).value
        self.assertEqual((result.status,result.error,result.image_id,result.attempts),('skipped','duplicate_sha256',None,0))
        self.write(lambda db: cmd.transition_source(db,source.source_id,expected='running',target='done',reason='complete'))
        nai,_ = self.source(kind='nai')
        with self.assertRaises(StateConflict):
            self.write(lambda db: cmd.skip_duplicate(db,self.items(nai)[0].item_id,sha256=image.sha256))
        with self.assertRaises(ConstraintViolation):
            self.write(lambda db: db.execute("UPDATE source_items SET error='other' WHERE item_id=?",(item.item_id,)))

    def test_D05_annotation_shape_current(self):
        image = self.image(('a',))
        def check(db):
            for content in ('{}','invalid','1'):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute('UPDATE annotations SET content=?',(content,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO annotations(image_id,source,type,content,created_at) VALUES(?,'original','tags','[]','t')",(image.image_id,))
            db.execute("INSERT INTO annotations(image_id,source,type,content,created_at,is_current) VALUES(?,'original','tags','[]','t',0)",(image.image_id,))
            db.execute("INSERT INTO annotations(image_id,source,type,content,created_at) VALUES(?,'original','nl','text','t')",(image.image_id,))
        self.write(check)
        for value in ((Tag('',None),),(Tag('a',float('nan')),),(Tag('a',1.1),),({'tag':'a'},)):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                tags(value)

    def test_D06_hash_paths_defaults(self):
        a = self.image()
        b = self.image()
        self.assertEqual(a.sha256,b.sha256)
        self.assertFalse(a.file_missing)
        self.assertEqual(self.read(lambda db: schemes.get_export_scheme(db,1)).pipeline_json,())
        for column,value in [('filename',a.filename),('sha256','x'*64),('width',0),('file_size',0)]:
            with self.subTest(column=column), self.assertRaises(ConstraintViolation):
                self.write(lambda db,c=column,v=value: db.execute(f'UPDATE images SET {c}=? WHERE image_id=?',(v,b.image_id)))
        for value in ('C:/x','//server/x','../x','a/../b','a\\b','/x','a:stream','a//b'):
            with self.subTest(value=value), self.assertRaises(UnsafePath):
                relative_path(self.root,value)
        for value in (True,1.2,'1'):
            with self.assertRaises(ValidationError):
                integer(value)
        with self.assertRaises(ValidationError):
            dumps({'bad':float('inf')})

    def test_D07_rating_tuple(self):
        image = self.image()
        for sql in ("rating='general'","rating='general',rating_source='fallback',rating_verified=1,rated_at='t'",
                    "rating='general',rating_source='manual',rating_verified=0,rated_at='t'",
                    "rating='questionable',rating_source='refusal',rating_verified=0,rated_at='t'"):
            with self.subTest(sql=sql), self.assertRaises(ConstraintViolation):
                self.write(lambda db,s=sql: db.execute(f'UPDATE images SET {s} WHERE image_id=?',(image.image_id,)))
        value = RatingValue('general','model',True,'2026-01-01T00:00:00.000000Z')
        self.write(lambda db: images.set_rating(db,image.image_id,expected=None,value=value))
        cleared = self.write(lambda db: images.set_rating(db,image.image_id,expected=value,value=None)).value
        self.assertEqual((cleared.rating,cleared.rating_source,cleared.rating_verified,cleared.rated_at),(None,None,False,None))
